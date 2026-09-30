"""Test fixtures for navig-mobile.

Isolates the device-inventory DB via ``NAVIG_DATA_DIR`` (per the house rule —
never ``NAVIG_HOME``) so tests never touch a real ~/.navig, and provides fake
Android/iOS backends so the whole surface is testable without a physical device.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Point all navig data at a throwaway dir BEFORE anything imports the store.
_TMP = Path(tempfile.mkdtemp(prefix="navig-mobile-test-"))
os.environ["NAVIG_DATA_DIR"] = str(_TMP)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Give every test a fresh device DB + mobile dir so store/consent/case state
    never leaks across tests (the singleton + on-disk DB would otherwise persist)."""
    import navig_mobile.config as cfg
    import navig_mobile.store as store

    store._STORE = None
    db = tmp_path / "mobile.db"
    mob = tmp_path / "mobile"
    mob.mkdir(exist_ok=True)
    monkeypatch.setattr(cfg, "db_path", lambda: db)
    monkeypatch.setattr(cfg, "mobile_dir", lambda: mob)
    yield
    store._STORE = None


# ── Fake Android raw device (duck-types adbutils' AdbDevice) ─────────────────

class FakeAdbRaw:
    def __init__(self, serial: str = "ABC123"):
        self.serial = serial
        self.sync = self  # so `_raw.sync.pull/push` resolve here
        self.installed: str | None = None
        self.uninstalled: str | None = None
        self.pushed: tuple[str, str] | None = None
        self._props = {
            "ro.product.model": "Pixel 8",
            "ro.product.name": "shiba",
            "ro.build.version.release": "15",
            "ro.product.manufacturer": "Google",
            "ro.serialno": "ABC123",
            "ro.product.device": "shiba",
            "ro.build.tags": "release-keys",
        }

    def getprop(self, name: str) -> str:
        return self._props.get(name, "")

    def shell(self, cmd: str) -> str:
        if cmd.startswith("dumpsys battery"):
            return "Current Battery Service state:\n  level: 87\n  scale: 100"
        if cmd == "df /data":
            return ("Filesystem     1K-blocks     Used Available Use% Mounted on\n"
                    "/dev/block/dm  100000000 40000000  60000000  40% /data")
        if cmd == "which su":
            return ""
        if cmd == "pm list packages -3":
            return "package:com.foo.app\npackage:com.bar.app"
        if cmd == "pm list packages":
            return ("package:com.foo.app\npackage:com.bar.app\n"
                    "package:android\npackage:com.android.systemui")
        if cmd.startswith("pm list packages com.foo.app"):
            return "package:com.foo.app"
        if cmd.startswith("pm path com.foo.app"):
            return "package:/data/app/com.foo.app-1/base.apk"
        if cmd.startswith("ls -la"):
            return ("total 8\n"
                    "drwxr-xr-x 2 root root 4096 2026-01-01 12:00 Download\n"
                    "-rw-r--r-- 1 root root  123 2026-01-01 12:00 file.txt")
        if cmd.startswith("logcat"):
            return "01-01 12:00:00.000  1000 1000 I Test: hello"
        return ""

    # install / uninstall
    def install(self, path: str) -> None:
        self.installed = path

    def uninstall(self, pkg: str) -> None:
        self.uninstalled = pkg

    def package_info(self, pkg: str):
        return {"version_name": "1.2.3"} if pkg == "com.foo.app" else None

    # sync.pull / sync.push
    def pull(self, remote: str, dst) -> int:
        Path(dst).write_bytes(b"\x89PNG\r\n")
        return 6

    def push(self, src, dst) -> None:
        self.pushed = (str(src), str(dst))


# ── Fake iOS CLI runner (duck-types engine.ios.device._run) ──────────────────

def fake_ios_runner(args, *, timeout: int = 60, parse_json: bool = False):
    group = args[0] if args else ""
    sub = args[1] if len(args) > 1 else ""
    if group == "lockdown" and sub == "info":
        return {
            "DeviceName": "My iPhone", "ProductType": "iPhone15,2",
            "ProductVersion": "17.5", "SerialNumber": "F2LABC",
            "UniqueDeviceID": "UDID123", "BatteryCurrentCapacity": 72,
            "TotalDiskCapacity": 128_000_000_000, "TotalDataAvailable": 64_000_000_000,
        }
    if group == "apps" and sub == "list":
        return {
            "com.foo.App": {"CFBundleDisplayName": "Foo", "CFBundleShortVersionString": "2.0",
                            "ApplicationType": "User", "Path": "/private/var/.../Foo.app"},
            "com.apple.Maps": {"CFBundleDisplayName": "Maps", "CFBundleShortVersionString": "1.0",
                               "ApplicationType": "System", "Path": "/Applications/Maps.app"},
        }
    if group == "afc" and sub == "ls":
        return "DCIM/\nPhotos.sqlite\n"
    if group == "afc" and sub == "pull":
        Path(args[3]).write_bytes(b"payload")
        return ""
    if group == "usbmux" and sub == "list":
        return [{"Identifier": "UDID123", "DeviceName": "My iPhone",
                 "ProductType": "iPhone15,2", "ProductVersion": "17.5",
                 "ConnectionType": "USB", "SerialNumber": "F2LABC"}]
    return ""


@pytest.fixture
def fake_android():
    return FakeAdbRaw()


@pytest.fixture
def fake_ios_run():
    return fake_ios_runner
