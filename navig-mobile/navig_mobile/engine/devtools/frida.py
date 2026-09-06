"""frida / objection wrappers — dynamic instrumentation for dev/rooted devices.

Both are detected and driven as subprocesses (frida-tools ships the ``frida`` /
``frida-ps`` / ``frida-ls-devices`` CLIs; objection is GPLv3). Attaching also
needs a matching ``frida-server`` running on a rooted/jailbroken device — we say
so honestly rather than pretending it works out of the box.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess

from navig.core.proc_text import decode_console_result
from navig_mobile.engine.base import DeviceError


class FridaUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__(
            "frida not found — install it (pip install frida-tools). Attaching also "
            "needs a matching frida-server running on the (rooted/jailbroken) device. "
            "See `navig mobile doctor`."
        )


def _tool(name: str) -> str | None:
    return shutil.which(name)


def available() -> bool:
    return _tool("frida-ps") is not None or importlib.util.find_spec("frida") is not None


def _require(name: str) -> str:
    exe = _tool(name)
    if not exe:
        raise FridaUnavailable()
    return exe


def _run(tool: str, args: list[str], *, timeout: int = 60) -> tuple[int, str]:
    """Run a capturing frida tool (e.g. frida-ps). Isolated for test monkeypatching."""
    exe = _require(tool)
    proc = decode_console_result(subprocess.run([exe, *args], capture_output=True, timeout=timeout))
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def ls_devices() -> tuple[int, str]:
    return _run("frida-ls-devices", [])


def ps(udid: str | None = None, *, apps: bool = True) -> tuple[int, str]:
    """List processes/apps on the USB device (`frida-ps -Uai`)."""
    args = ["-D", udid] if udid else ["-U"]
    if apps:
        args.append("-ai")
    return _run("frida-ps", args)


def attach(target: str, udid: str | None = None) -> int:
    """Attach an interactive frida session to a running process (by name)."""
    exe = _require("frida")
    argv = [exe] + (["-D", udid] if udid else ["-U"]) + ["-n", target]
    return subprocess.run(argv).returncode


def spawn(target: str, udid: str | None = None) -> int:
    """Spawn an app under frida (interactive)."""
    exe = _require("frida")
    argv = [exe] + (["-D", udid] if udid else ["-U"]) + ["-f", target]
    return subprocess.run(argv).returncode


def objection(target: str, udid: str | None = None) -> int:
    """Launch objection's interactive explorer against an app (GPLv3 — subprocess)."""
    exe = shutil.which("objection")
    if not exe:
        raise DeviceError(
            "objection not found — install it (pip install objection). "
            "It drives frida; the device needs a matching frida-server."
        )
    argv = [exe, "-g", target, "explore"]
    return subprocess.run(argv).returncode
