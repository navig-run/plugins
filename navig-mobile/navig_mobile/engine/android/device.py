"""Android device engine — wraps ``adbutils`` for the covered surface and shells
out to the ``adb`` binary for operations adbutils doesn't wrap (backup).

``AndroidDevice`` takes an injected raw adbutils device object, so it is fully
unit-testable with a fake (no adb server required).
"""

from __future__ import annotations

import functools
import importlib.util
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from navig_mobile.engine.base import (
    AppInfo,
    Connection,
    DeviceInfo,
    DeviceError,
    Platform,
    PlatformUnavailableError,
    ProgressCb,
)

_INSTALL_HINT = 'pip install "navig-mobile[android]"'

# `adb backup` exits 0 even when the on-device prompt is declined or times out — it
# then writes nothing or only a tiny (~24-byte) archive header. A real
# `-all -shared -apk` archive is many KB even on a near-empty device, so anything
# under this floor means "no data was captured" and must not be reported as a backup.
_MIN_BACKUP_BYTES = 1024


def _wrap_adb_errors(fn):
    """Convert raw adbutils errors (AdbError/AdbTimeout/OSError on a mid-op
    disconnect) into ``DeviceError`` so command handlers — which catch only
    ``DeviceError`` — surface a friendly message instead of a traceback."""

    @functools.wraps(fn)
    def inner(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (DeviceError, PlatformUnavailableError):
            raise
        except Exception as exc:  # adbutils AdbError/AdbTimeout, OSError, etc.
            raise DeviceError(f"adb error: {exc}") from exc

    return inner


def _shq(path: str) -> str:
    """Quote a path for the on-device shell so spaces work (`ls /sdcard/My Dir`).
    Not injection protection — the user already owns their own device shell."""
    if path and all(ch.isalnum() or ch in "/._-@+" for ch in path):
        return path
    return "'" + path.replace("'", "'\\''") + "'"


def ensure_available() -> None:
    if importlib.util.find_spec("adbutils") is None:
        raise PlatformUnavailableError(Platform.ANDROID, _INSTALL_HINT)


def _client():
    ensure_available()
    import adbutils

    return adbutils.AdbClient()


def list_devices() -> list[DeviceInfo]:
    """Discover attached Android devices. Returns [] if adb server/binary is
    unavailable (so cross-platform discovery still works)."""
    try:
        client = _client()
        raws = client.device_list()
    except PlatformUnavailableError:
        raise
    except Exception:
        return []
    out: list[DeviceInfo] = []
    for raw in raws:
        try:
            out.append(AndroidDevice(raw, raw.serial).quick_info())
        except Exception:
            out.append(DeviceInfo(udid=getattr(raw, "serial", "?"),
                                  platform=Platform.ANDROID,
                                  connection=_conn(getattr(raw, "serial", ""))))
    return out


def get_device(udid: str) -> "AndroidDevice":
    client = _client()
    raw = client.device(serial=udid)
    return AndroidDevice(raw, udid)


def _conn(serial: str) -> Connection:
    # TCP/IP-attached devices carry "host:port" serials.
    return Connection.NETWORK if ":" in (serial or "") else Connection.USB


class AndroidDevice:
    platform = Platform.ANDROID

    def __init__(self, raw: Any, udid: str):
        self._raw = raw
        self.udid = udid

    # ── info ────────────────────────────────────────────────────────────────
    def _prop(self, name: str) -> str:
        try:
            return (self._raw.getprop(name) or "").strip()
        except Exception:
            return ""

    def quick_info(self) -> DeviceInfo:
        """Lightweight info for device listing (a couple of props)."""
        return DeviceInfo(
            udid=self.udid,
            platform=Platform.ANDROID,
            name=self._prop("ro.product.model") or self._prop("ro.product.name"),
            model=self._prop("ro.product.model"),
            os_version=self._prop("ro.build.version.release"),
            manufacturer=self._prop("ro.product.manufacturer"),
            connection=_conn(self.udid),
            developer_mode=True,  # we're speaking ADB → USB debugging is on
        )

    def info(self) -> DeviceInfo:
        di = self.quick_info()
        di.serial = self._prop("ro.serialno") or self.udid
        di.product_type = self._prop("ro.product.device")
        di.battery = self._battery()
        total, free = self._storage()
        di.storage_total, di.storage_free = total, free
        di.rooted_or_jailbroken = self._rooted()
        return di

    def _battery(self) -> int | None:
        try:
            out = self._raw.shell("dumpsys battery")
            m = re.search(r"level:\s*(\d+)", out)
            return int(m.group(1)) if m else None
        except Exception:
            return None

    def _storage(self) -> tuple[int | None, int | None]:
        try:
            out = self._raw.shell("df /data")
            # ...  1K-blocks  Used  Available ...
            for line in out.splitlines():
                if line.strip().startswith("/") or "/data" in line:
                    parts = line.split()
                    nums = [p for p in parts if p.isdigit()]
                    if len(nums) >= 3:
                        total = int(nums[0]) * 1024
                        free = int(nums[2]) * 1024
                        return total, free
        except Exception:
            pass
        return None, None

    def _rooted(self) -> bool | None:
        try:
            if (self._raw.shell("which su") or "").strip():
                return True
            return "test-keys" in self._prop("ro.build.tags")
        except Exception:
            return None

    # ── apps ────────────────────────────────────────────────────────────────
    def _packages(self, *, third_party: bool) -> list[str]:
        flag = " -3" if third_party else ""
        out = self._raw.shell(f"pm list packages{flag}")
        return sorted(p.split(":", 1)[1].strip()
                      for p in out.splitlines() if p.startswith("package:"))

    @_wrap_adb_errors
    def apps(self, *, system: bool = False) -> list[AppInfo]:
        third = set(self._packages(third_party=True))
        allp = set(self._packages(third_party=False)) | third
        pkgs = allp if system else third
        return [AppInfo(app_id=p, name=p, is_system=(p not in third))
                for p in sorted(pkgs)]

    def app_info(self, app_id: str) -> AppInfo | None:
        try:
            info = None
            try:
                info = self._raw.package_info(app_id)
            except Exception:
                info = None
            if not info:
                # Confirm existence via pm.
                listed = self._raw.shell(f"pm list packages {app_id}")
                if f"package:{app_id}" not in listed:
                    return None
            path = ""
            try:
                pout = self._raw.shell(f"pm path {app_id}")
                path = pout.split("package:", 1)[1].strip() if "package:" in pout else ""
            except Exception:
                path = ""
            third = set(self._packages(third_party=True))
            version = ""
            if isinstance(info, dict):
                version = str(info.get("version_name") or info.get("versionName") or "")
            return AppInfo(app_id=app_id, name=app_id, version=version, path=path,
                           is_system=(app_id not in third))
        except Exception:
            return None

    @_wrap_adb_errors
    def install_app(self, path: str) -> None:
        self._raw.install(path)

    @_wrap_adb_errors
    def uninstall_app(self, app_id: str) -> None:
        self._raw.uninstall(app_id)

    @_wrap_adb_errors
    def extract_app(self, app_id: str, dest: str) -> str:
        """Pull the installed base APK back to disk (3uTools-style app backup)."""
        out = self._raw.shell(f"pm path {app_id}")
        remote = ""
        for line in out.splitlines():
            if line.startswith("package:"):
                remote = line.split("package:", 1)[1].strip()
                break
        if not remote:
            raise DeviceError(f"App {app_id!r} not found on device.")
        dpath = Path(dest)
        if dpath.is_dir():
            dpath = dpath / f"{app_id}.apk"
        self._raw.sync.pull(remote, str(dpath))
        return str(dpath)

    # ── files ───────────────────────────────────────────────────────────────
    @_wrap_adb_errors
    def ls(self, remote: str = "/") -> list[dict[str, Any]]:
        # -L dereferences symlinks so a symlinked directory (e.g. /sdcard →
        # /storage/self/primary) lists its *contents*, not the link itself.
        out = self._raw.shell(f"ls -laL {_shq(remote)}")
        entries: list[dict[str, Any]] = []
        for line in out.splitlines():
            line = line.rstrip()
            if not line or line.startswith("total"):
                continue
            if line.startswith("ls:") or "No such file" in line or "Permission denied" in line:
                continue
            parts = line.split()
            if len(parts) < 7 or line[0] not in "dlpbcs-":
                continue  # not a valid `ls -l` mode line
            # symlink lines read "<perms> … <name> -> <target>": take the name.
            name = parts[parts.index("->") - 1] if "->" in parts else parts[-1]
            entries.append({
                "name": name,
                "mode": parts[0],
                "size": int(parts[4]) if parts[4].isdigit() else None,
                "is_dir": line[0] == "d",
            })
        return entries

    @_wrap_adb_errors
    def pull(self, remote: str, local: str, progress: ProgressCb | None = None) -> str:
        dpath = Path(local)
        if dpath.is_dir():
            dpath = dpath / Path(remote).name
        n = self._raw.sync.pull(remote, str(dpath))
        if progress:
            progress(int(n or 0), int(n or 0), Path(remote).name)
        return str(dpath)

    @_wrap_adb_errors
    def push(self, local: str, remote: str) -> None:
        self._raw.sync.push(local, remote)

    # ── screen ──────────────────────────────────────────────────────────────
    @_wrap_adb_errors
    def screenshot(self, dest: str) -> str:
        remote = "/sdcard/navig_mobile_screen.png"
        self._raw.shell(f"screencap -p {remote}")
        dpath = Path(dest)
        if dpath.is_dir():
            dpath = dpath / "screenshot.png"
        self._raw.sync.pull(remote, str(dpath))
        try:
            self._raw.shell(f"rm -f {remote}")
        except Exception:
            pass
        return str(dpath)

    # ── backup ──────────────────────────────────────────────────────────────
    @_wrap_adb_errors
    def backup(self, dest: str, *, encrypted: bool = False,
               progress: ProgressCb | None = None) -> str:
        """``adb backup`` — verifies a real archive landed before returning.

        NOTE: adb backup is deprecated and largely neutered on Android 12+ (needs
        an on-device confirmation and skips most app data). Use `forensics acquire`
        for a real logical extraction.

        Raises ``DeviceError`` if adb fails OR (adb exits 0 even on a declined
        prompt) if no real archive was written — so a caller never records or
        reports a phantom backup right before the wipe/root flows that tell users
        to back up first.
        """
        adb = shutil.which("adb")
        if not adb:
            raise DeviceError(
                "The `adb` binary is required for Android backup — install "
                "platform-tools (see `navig mobile doctor`)."
            )
        dpath = Path(dest)
        if dpath.is_dir():
            dpath = dpath / f"{self.udid.replace(':', '_')}.ab"
        argv = [adb, "-s", self.udid, "backup", "-apk", "-shared", "-all",
                "-f", str(dpath)]
        if progress:
            progress(0, 0, "confirm the backup prompt on the device…")
        rc = subprocess.run(argv, timeout=1800).returncode
        if rc != 0:
            raise DeviceError(f"adb backup failed (exit {rc}).")
        size = dpath.stat().st_size if dpath.exists() else 0
        if size < _MIN_BACKUP_BYTES:
            raise DeviceError(
                "adb backup produced no data — the on-device backup prompt was "
                "likely declined or timed out. Confirm the prompt on the device "
                "and retry. (adb backup is also neutered on Android 12+; prefer "
                "`navig mobile forensics acquire` for a real extraction.)"
            )
        return str(dpath)

    @_wrap_adb_errors
    def restore(self, src: str, *, password: str | None = None,  # noqa: ARG002
                progress: ProgressCb | None = None) -> None:
        """``adb restore <file.ab>`` — overwrites app data (on-device confirm).
        DESTRUCTIVE; deprecated on Android 12+. Password isn't used by adb restore."""
        adb = shutil.which("adb")
        if not adb:
            raise DeviceError(
                "The `adb` binary is required for Android restore — install "
                "platform-tools (see `navig mobile doctor`)."
            )
        spath = Path(src)
        if spath.is_dir():
            cands = sorted(spath.glob("*.ab"))
            if not cands:
                raise DeviceError(f"No .ab backup file found in {src}.")
            spath = cands[-1]
        if not spath.exists():
            raise DeviceError(f"Backup file not found: {src}")
        if progress:
            progress(0, 0, "confirm the restore prompt on the device…")
        rc = subprocess.run([adb, "-s", self.udid, "restore", str(spath)], timeout=1800).returncode
        if rc != 0:
            raise DeviceError(f"adb restore failed (exit {rc}).")

    @_wrap_adb_errors
    def reboot(self, mode: str = "system") -> None:
        arg = {"system": "", "recovery": "recovery", "bootloader": "bootloader",
               "fastboot": "bootloader"}.get(mode, "")
        self._raw.shell(f"reboot {arg}".strip())
