"""iOS device engine — a thin, version-robust wrapper over the ``pymobiledevice3``
CLI (invoked as ``python -m pymobiledevice3``). The CLI is stable across the fast-
moving v9 async Python API and is the natural "3uTools shells out" surface.

``IosDevice`` takes an injectable ``runner`` so it is unit-testable without a
device or the pymobiledevice3 package installed.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from navig_mobile.engine.base import (
    AppInfo,
    Connection,
    DeviceInfo,
    DeviceError,
    Platform,
    PlatformUnavailableError,
    ProgressCb,
)

_INSTALL_HINT = 'pip install "navig-mobile[ios]"'
Runner = Callable[..., str]


def ensure_available() -> None:
    if importlib.util.find_spec("pymobiledevice3") is None and not shutil.which("pymobiledevice3"):
        raise PlatformUnavailableError(Platform.IOS, _INSTALL_HINT)


def _cli() -> list[str]:
    """Prefer the module invocation (no PATH dependency); fall back to the
    console script if the module isn't importable in this interpreter."""
    if importlib.util.find_spec("pymobiledevice3") is not None:
        return [sys.executable, "-m", "pymobiledevice3"]
    exe = shutil.which("pymobiledevice3")
    if exe:
        return [exe]
    raise PlatformUnavailableError(Platform.IOS, _INSTALL_HINT)


def _run(args: list[str], *, timeout: int = 60, parse_json: bool = False) -> Any:
    """Run a pymobiledevice3 CLI command. Returns stdout (or parsed JSON)."""
    argv = [*_cli(), *args]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise DeviceError(f"pymobiledevice3 timed out: {' '.join(args)}") from exc
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip().splitlines()
        detail = msg[-1] if msg else f"exit {proc.returncode}"
        raise DeviceError(f"pymobiledevice3 {' '.join(args[:2])} failed: {detail}")
    out = proc.stdout or ""
    if parse_json:
        try:
            return json.loads(out)
        except Exception as exc:
            raise DeviceError("pymobiledevice3 returned non-JSON output.") from exc
    return out


def list_devices() -> list[DeviceInfo]:
    """Discover attached iOS devices via ``usbmux list``. Returns [] on any
    backend error (missing usbmux driver, no device) so cross-platform discovery
    keeps working."""
    try:
        ensure_available()
    except PlatformUnavailableError:
        raise
    try:
        data = _run(["usbmux", "list"], parse_json=True, timeout=30)
    except Exception:
        return []
    out: list[DeviceInfo] = []
    for item in data if isinstance(data, list) else []:
        udid = (item.get("Identifier") or item.get("UniqueDeviceID")
                or item.get("SerialNumber") or "")
        if not udid:
            continue
        conn = Connection.NETWORK if str(item.get("ConnectionType", "")).lower() == "network" \
            else Connection.USB
        out.append(DeviceInfo(
            udid=udid,
            platform=Platform.IOS,
            name=item.get("DeviceName", "") or "",
            model=item.get("ProductType", "") or "",
            os_version=item.get("ProductVersion", "") or "",
            product_type=item.get("ProductType", "") or "",
            serial=item.get("SerialNumber", "") or "",
            connection=conn,
            extra={"raw": item},
        ))
    return out


def get_device(udid: str) -> "IosDevice":
    ensure_available()
    return IosDevice(udid)


class IosDevice:
    platform = Platform.IOS

    def __init__(self, udid: str, runner: Runner | None = None):
        self.udid = udid
        self._run: Runner = runner or _run

    def _dev_args(self, args: list[str]) -> list[str]:
        return [*args, "--udid", self.udid]

    # ── info ────────────────────────────────────────────────────────────────
    def info(self) -> DeviceInfo:
        data = self._run(self._dev_args(["lockdown", "info"]), parse_json=True, timeout=30)
        if not isinstance(data, dict):
            data = {}
        # Battery lives in the com.apple.mobile.battery domain, not the default
        # value set — fetch it best-effort so `navig mobile battery` works on HW.
        battery = data.get("BatteryCurrentCapacity")
        if battery is None:
            try:
                bat = self._run(self._dev_args(["lockdown", "info", "-d",
                                                "com.apple.mobile.battery"]),
                                parse_json=True, timeout=15)
                if isinstance(bat, dict):
                    battery = bat.get("BatteryCurrentCapacity")
            except Exception:
                battery = None  # best-effort; never fail info() over battery
        di = DeviceInfo(
            udid=self.udid,
            platform=Platform.IOS,
            name=data.get("DeviceName", "") or "",
            model=data.get("ProductType", "") or "",
            os_version=data.get("ProductVersion", "") or "",
            serial=data.get("SerialNumber", "") or "",
            manufacturer="Apple",
            product_type=data.get("ProductType", "") or "",
            battery=int(battery) if isinstance(battery, (int, float)) else None,
            storage_total=_int(data.get("TotalDiskCapacity")),
            storage_free=_int(data.get("TotalDataAvailable")),
            trusted=True,  # lockdown info succeeded → paired/trusted
            connection=Connection.USB,
            extra={"UniqueChipID": data.get("UniqueChipID")},
        )
        return di

    # ── apps ────────────────────────────────────────────────────────────────
    def apps(self, *, system: bool = False) -> list[AppInfo]:
        app_type = "Any" if system else "User"
        data = self._run(self._dev_args(["apps", "list", "--type", app_type]),
                         parse_json=True, timeout=60)
        out: list[AppInfo] = []
        for bundle_id, meta in (data or {}).items():
            meta = meta if isinstance(meta, dict) else {}
            is_sys = str(meta.get("ApplicationType", "")).lower() == "system"
            out.append(AppInfo(
                app_id=bundle_id,
                name=meta.get("CFBundleDisplayName") or meta.get("CFBundleName") or bundle_id,
                version=str(meta.get("CFBundleShortVersionString")
                            or meta.get("CFBundleVersion") or ""),
                is_system=is_sys,
                path=meta.get("Path", "") or "",
            ))
        return sorted(out, key=lambda a: a.name.lower())

    def app_info(self, app_id: str) -> AppInfo | None:
        for a in self.apps(system=True):
            if a.app_id == app_id:
                return a
        return None

    def install_app(self, path: str) -> None:
        self._run(self._dev_args(["apps", "install", path]), timeout=600)

    def uninstall_app(self, app_id: str) -> None:
        self._run(self._dev_args(["apps", "uninstall", app_id]), timeout=120)

    def extract_app(self, app_id: str, dest: str) -> str:
        """iOS can't hand back a decrypted IPA; ``apps pull`` exports the app's
        data container instead. The destination is a directory."""
        dpath = Path(dest)
        dpath.mkdir(parents=True, exist_ok=True)
        self._run(self._dev_args(["apps", "pull", app_id, str(dpath)]), timeout=600)
        return str(dpath)

    # ── files (AFC — media partition) ───────────────────────────────────────
    def ls(self, remote: str = "/") -> list[dict[str, Any]]:
        out = self._run(self._dev_args(["afc", "ls", remote]), timeout=60)
        entries: list[dict[str, Any]] = []
        for line in str(out).splitlines():
            name = line.strip()
            if name:
                entries.append({"name": name, "is_dir": name.endswith("/")})
        return entries

    def pull(self, remote: str, local: str, progress: ProgressCb | None = None) -> str:
        dpath = Path(local)
        if dpath.is_dir():
            dpath = dpath / Path(remote).name
        self._run(self._dev_args(["afc", "pull", remote, str(dpath)]), timeout=600)
        if progress:
            size = dpath.stat().st_size if dpath.exists() else 0
            progress(size, size, Path(remote).name)
        return str(dpath)

    def push(self, local: str, remote: str) -> None:
        self._run(self._dev_args(["afc", "push", local, remote]), timeout=600)

    # ── screen ──────────────────────────────────────────────────────────────
    def screenshot(self, dest: str) -> str:
        dpath = Path(dest)
        if dpath.is_dir():
            dpath = dpath / "screenshot.png"
        try:
            self._run(self._dev_args(["developer", "screenshot", str(dpath)]), timeout=60)
        except DeviceError as exc:
            raise DeviceError(
                "iOS screenshot needs Developer Mode + a mounted Developer Disk "
                "Image (iOS 17+ also needs an elevated `pymobiledevice3 remote "
                "tunneld`). Original error: " + str(exc)
            ) from exc
        return str(dpath)

    # ── backup ──────────────────────────────────────────────────────────────
    def backup(self, dest: str, *, encrypted: bool = False,
               progress: ProgressCb | None = None) -> str:
        dpath = Path(dest)
        dpath.mkdir(parents=True, exist_ok=True)
        if progress:
            progress(0, 0, "starting iOS backup (mobilebackup2)…")
        args = ["backup2", "backup", "--full", str(dpath)]
        # NOTE: enabling/using backup encryption is handled in a later stage
        # (password → vault). `encrypted` is accepted for API parity.
        self._run(self._dev_args(args), timeout=3600)
        return str(dpath)

    def restore(self, src: str, *, password: str | None = None,
                progress: ProgressCb | None = None) -> None:
        """``backup2 restore`` — DESTRUCTIVE (overwrites device data, reboots).
        ``src`` is the parent dir holding a ``<udid>/`` backup."""
        if progress:
            progress(0, 0, "restoring iOS backup (mobilebackup2)…")
        args = ["backup2", "restore", str(src)]
        if password:
            args += ["--password", password]
        self._run(self._dev_args(args), timeout=3600)

    def reboot(self, mode: str = "system") -> None:
        cmd = {"system": "restart", "shutdown": "shutdown"}.get(mode, "restart")
        self._run(self._dev_args(["diagnostics", cmd]), timeout=30)

    def lockdown_values(self) -> dict[str, Any]:
        """Full lockdown value dict (device identity, IMEI, MACs, etc.)."""
        data = self._run(self._dev_args(["lockdown", "info"]), parse_json=True, timeout=30)
        return data if isinstance(data, dict) else {}


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
