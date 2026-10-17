"""VMware Workstation, driven through its own command-line tools.

``vmrun`` starts, stops, snapshots and asks a VM for its IP (through VMware Tools in the
guest); ``vmware-vdiskmanager`` converts and grows disks. Both ship with Workstation and
are usually NOT on PATH, so they are looked up in the install directory too.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from navig_sdk.host import decode_console_result

try:  # navig present: its one truth table for console-window flags
    from navig.platform.process import creation_flags as _creation_flags
except ImportError:  # standalone navig-vm

    def _creation_flags() -> int:
        return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def run_text(args: list[str], timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    """Run a VMware tool and return its decoded output — through FILES, never pipes.

    `vmrun start <vmx> gui` launches the VMware window, and the window inherits the
    caller's stdout/stderr. With pipes, vmrun exits but the window keeps the write end
    open for as long as it runs, so reading to EOF never finishes — and a timeout does
    not help, because after killing vmrun the read still waits on the window's copy.
    Measured: `navig vm create` hung for as long as the VM ran. A file has no EOF to
    wait for.
    """
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as errf:
        proc = subprocess.run(  # noqa: S603 — argv built from a located tool, no shell
            args,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=errf,
            timeout=timeout,
            check=False,
            creationflags=_creation_flags(),
        )
        out.seek(0)
        errf.seek(0)
        raw = SimpleNamespace(args=args, returncode=proc.returncode, stdout=out.read(), stderr=errf.read())
    return decode_console_result(raw)

_WINDOWS_DIRS = (
    r"C:\Program Files (x86)\VMware\VMware Workstation",
    r"C:\Program Files\VMware\VMware Workstation",
)
_POSIX_DIRS = ("/usr/bin", "/usr/local/bin", "/Applications/VMware Fusion.app/Contents/Library")


class VMwareError(RuntimeError):
    """vmrun / vdiskmanager failed; the message is what the tool said."""


def find_tool(name: str) -> Path | None:
    """``vmrun`` or ``vmware-vdiskmanager``: PATH first, then the Workstation install dir."""
    exe = f"{name}.exe" if sys.platform == "win32" else name
    on_path = shutil.which(exe)
    if on_path:
        return Path(on_path)
    dirs = list(_WINDOWS_DIRS if sys.platform == "win32" else _POSIX_DIRS)
    override = os.environ.get("NAVIG_VMWARE_DIR", "").strip()
    if override:
        dirs.insert(0, override)
    for d in dirs:
        candidate = Path(d) / exe
        if candidate.is_file():
            return candidate
    return None


@dataclass
class VMware:
    vmrun: Path
    vdiskmanager: Path | None = None

    @classmethod
    def locate(cls) -> VMware:
        vmrun = find_tool("vmrun")
        if vmrun is None:
            raise VMwareError(
                "VMware Workstation's vmrun was not found. Install VMware Workstation, or set "
                "NAVIG_VMWARE_DIR to its install folder."
            )
        return cls(vmrun=vmrun, vdiskmanager=find_tool("vmware-vdiskmanager"))

    # ── vmrun ──────────────────────────────────────────────────────────────

    def _vmrun(self, *args: str, timeout: float = 120.0, check: bool = True) -> str:
        argv = [str(self.vmrun), "-T", "ws", *args]
        try:
            proc = run_text(argv, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise VMwareError(f"vmrun {args[0]} did not finish within {timeout:g}s") from exc
        out = (proc.stdout or "").strip()
        # vmrun reports most failures as "Error: …" on stdout with exit 255.
        if check and (proc.returncode != 0 or out.startswith("Error:")):
            reason = out or (proc.stderr or "").strip() or f"exit {proc.returncode}"
            raise VMwareError(f"vmrun {args[0]}: {reason}")
        return out

    def start(self, vmx: Path, gui: bool = True) -> None:
        # Starting a VM just reverted to a snapshot took over 3 minutes, measured.
        self._vmrun("start", str(vmx), "gui" if gui else "nogui", timeout=600)

    def stop(self, vmx: Path, hard: bool = False) -> None:
        self._vmrun("stop", str(vmx), "hard" if hard else "soft", timeout=180)

    def running(self) -> list[Path]:
        """The .vmx of every running VM (``vmrun list``)."""
        out = self._vmrun("list")
        lines = [ln.strip() for ln in out.splitlines()]
        return [Path(ln) for ln in lines[1:] if ln]

    def is_running(self, vmx: Path) -> bool:
        target = os.path.normcase(str(vmx.resolve()))
        return any(os.path.normcase(str(p)) == target for p in self.running())

    def guest_ip(self, vmx: Path, wait: bool = False, timeout: float = 30.0) -> str | None:
        """The guest's IP through VMware Tools, or None when the guest has not reported one."""
        args = ["getGuestIPAddress", str(vmx)] + (["-wait"] if wait else [])
        try:
            out = self._vmrun(*args, timeout=timeout)
        except VMwareError:
            return None
        ip = out.splitlines()[-1].strip() if out else ""
        # While the guest boots, Tools answers the literal word "unknown" (exit 0) — taken
        # for an address, ssh then tried to resolve a host called "unknown". Measured.
        import ipaddress

        try:
            return str(ipaddress.IPv4Address(ip))
        except ValueError:
            return None

    def snapshot(self, vmx: Path, name: str) -> None:
        # A running VM's snapshot includes its memory: 8 GB took over 5 minutes, measured.
        self._vmrun("snapshot", str(vmx), name, timeout=1800)

    def revert(self, vmx: Path, name: str) -> None:
        self._vmrun("revertToSnapshot", str(vmx), name, timeout=1800)

    def snapshots(self, vmx: Path) -> list[str]:
        out = self._vmrun("listSnapshots", str(vmx))
        return [ln.strip() for ln in out.splitlines()[1:] if ln.strip()]

    def delete_vm(self, vmx: Path) -> None:
        self._vmrun("deleteVM", str(vmx), timeout=300)

    def capture_screen(self, vmx: Path, out: Path, user: str, password: str) -> None:
        """A PNG of the guest's screen. Needs VMware Tools in the guest and a guest login."""
        self._vmrun("-gu", user, "-gp", password, "captureScreen", str(vmx), str(out), timeout=120)

    # ── vmware-vdiskmanager ────────────────────────────────────────────────

    def _vdisk(self, *args: str, timeout: float = 1800.0) -> None:
        if self.vdiskmanager is None:
            raise VMwareError("vmware-vdiskmanager was not found next to vmrun")
        try:
            proc = run_text([str(self.vdiskmanager), *args], timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise VMwareError(f"vmware-vdiskmanager {args[0]} did not finish within {timeout:g}s") from exc
        text = f"{proc.stdout}\n{proc.stderr}".strip()
        if proc.returncode != 0 or "Failed" in text:
            raise VMwareError(f"vmware-vdiskmanager {' '.join(args[:1])}: {text or proc.returncode}")

    def convert_to_growable(self, src: Path, dst: Path) -> None:
        """Any disk VMware can read → a single growable file (type 0)."""
        self._vdisk("-r", str(src), "-t", "0", str(dst))

    def expand(self, vmdk: Path, size_gb: int) -> None:
        self._vdisk("-x", f"{size_gb}GB", str(vmdk))
