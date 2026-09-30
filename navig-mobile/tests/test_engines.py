"""Engine wiring tests — Android (adbutils fake) + iOS (CLI-runner fake) +
DeviceManager resolution. No physical device required."""

from __future__ import annotations

from pathlib import Path

import pytest

from navig_mobile.engine.android.device import AndroidDevice
from navig_mobile.engine.base import (
    AmbiguousDeviceError,
    Device,
    DeviceError,
    DeviceInfo,
    DeviceManager,
    NoDeviceError,
    Platform,
)
from navig_mobile.engine.ios.device import IosDevice


# ── Android ──────────────────────────────────────────────────────────────────

def test_android_implements_protocol(fake_android):
    assert isinstance(AndroidDevice(fake_android, "ABC123"), Device)


def test_android_info(fake_android):
    dev = AndroidDevice(fake_android, "ABC123")
    info = dev.info()
    assert info.platform is Platform.ANDROID
    assert info.model == "Pixel 8"
    assert info.os_version == "15"
    assert info.battery == 87
    assert info.storage_total == 100_000_000 * 1024
    assert info.storage_free == 60_000_000 * 1024
    assert info.rooted_or_jailbroken is False
    assert info.developer_mode is True


def test_android_apps_user_vs_system(fake_android):
    dev = AndroidDevice(fake_android, "ABC123")
    user = dev.apps(system=False)
    assert {a.app_id for a in user} == {"com.foo.app", "com.bar.app"}
    assert all(not a.is_system for a in user)
    allapps = dev.apps(system=True)
    ids = {a.app_id for a in allapps}
    assert "android" in ids and "com.android.systemui" in ids
    assert any(a.is_system for a in allapps)


def test_android_app_info_and_extract(fake_android, tmp_path):
    dev = AndroidDevice(fake_android, "ABC123")
    ai = dev.app_info("com.foo.app")
    assert ai and ai.version == "1.2.3"
    assert ai.path.endswith("base.apk")
    saved = dev.extract_app("com.foo.app", str(tmp_path))
    assert Path(saved).exists() and saved.endswith("com.foo.app.apk")


def test_android_ls_pull_screenshot_install(fake_android, tmp_path):
    dev = AndroidDevice(fake_android, "ABC123")
    entries = dev.ls("/sdcard")
    names = {e["name"]: e["is_dir"] for e in entries}
    assert names.get("Download") is True and names.get("file.txt") is False
    out = dev.pull("/sdcard/file.txt", str(tmp_path))
    assert Path(out).exists()
    shot = dev.screenshot(str(tmp_path))
    assert Path(shot).exists() and shot.endswith(".png")
    dev.install_app("/tmp/x.apk")
    dev.uninstall_app("com.foo.app")
    assert fake_android.installed == "/tmp/x.apk"
    assert fake_android.uninstalled == "com.foo.app"


# ── iOS ──────────────────────────────────────────────────────────────────────

def test_ios_implements_protocol():
    assert isinstance(IosDevice("UDID123", runner=lambda *a, **k: ""), Device)


def test_ios_info(fake_ios_run):
    dev = IosDevice("UDID123", runner=fake_ios_run)
    info = dev.info()
    assert info.platform is Platform.IOS
    assert info.name == "My iPhone"
    assert info.model == "iPhone15,2"
    assert info.os_version == "17.5"
    assert info.battery == 72
    assert info.storage_total == 128_000_000_000
    assert info.manufacturer == "Apple"
    assert info.trusted is True


def test_ios_apps(fake_ios_run):
    dev = IosDevice("UDID123", runner=fake_ios_run)
    user = dev.apps(system=False)
    assert [a.app_id for a in user] == ["com.foo.App"] or "com.foo.App" in {a.app_id for a in user}
    allapps = dev.apps(system=True)
    ids = {a.app_id for a in allapps}
    assert "com.apple.Maps" in ids
    maps = next(a for a in allapps if a.app_id == "com.apple.Maps")
    assert maps.is_system is True
    assert maps.name == "Maps"


def test_ios_ls_and_pull(fake_ios_run, tmp_path):
    dev = IosDevice("UDID123", runner=fake_ios_run)
    entries = dev.ls("/")
    names = {e["name"] for e in entries}
    assert "DCIM/" in names and "Photos.sqlite" in names
    out = dev.pull("/DCIM/IMG_0001.JPG", str(tmp_path))
    assert Path(out).exists() and Path(out).read_bytes() == b"payload"


def test_ios_screenshot_error_is_friendly(monkeypatch, tmp_path):
    from navig_mobile.engine.base import DeviceError

    def boom(*a, **k):
        raise DeviceError("DeveloperMode not enabled")

    dev = IosDevice("UDID123", runner=boom)
    with pytest.raises(DeviceError) as ei:
        dev.screenshot(str(tmp_path))
    assert "Developer Mode" in str(ei.value)


# ── DeviceManager resolution ─────────────────────────────────────────────────

def _patch_discovery(monkeypatch, android=(), ios=()):
    import navig_mobile.engine.android.device as adev
    import navig_mobile.engine.ios.device as idev

    monkeypatch.setattr(adev, "ensure_available", lambda: None)
    monkeypatch.setattr(idev, "ensure_available", lambda: None)
    monkeypatch.setattr(adev, "list_devices", lambda: list(android))
    monkeypatch.setattr(idev, "list_devices", lambda: list(ios))
    monkeypatch.setattr(adev, "get_device", lambda u: ("android", u))
    monkeypatch.setattr(idev, "get_device", lambda u: ("ios", u))


def _info(udid, platform):
    return DeviceInfo(udid=udid, platform=platform, name=udid)


def test_manager_no_device_raises(monkeypatch):
    _patch_discovery(monkeypatch)
    with pytest.raises(NoDeviceError):
        DeviceManager().resolve()


def test_manager_single_autodetect(monkeypatch):
    _patch_discovery(monkeypatch, android=[_info("A1", Platform.ANDROID)])
    assert DeviceManager().resolve() == ("android", "A1")


def test_manager_ambiguous_raises(monkeypatch):
    _patch_discovery(monkeypatch, android=[_info("A1", Platform.ANDROID)],
                     ios=[_info("I1", Platform.IOS)])
    with pytest.raises(AmbiguousDeviceError):
        DeviceManager().resolve()


def test_manager_resolve_by_udid(monkeypatch):
    _patch_discovery(monkeypatch, android=[_info("A1", Platform.ANDROID)],
                     ios=[_info("I1", Platform.IOS)])
    assert DeviceManager().resolve(udid="I1") == ("ios", "I1")


def test_manager_lists_both_platforms(monkeypatch):
    _patch_discovery(monkeypatch, android=[_info("A1", Platform.ANDROID)],
                     ios=[_info("I1", Platform.IOS)])
    infos = DeviceManager().list_devices()
    assert {i.udid for i in infos} == {"A1", "I1"}


# ── restore (destructive) ────────────────────────────────────────────────────

def test_android_restore_args(monkeypatch, tmp_path, fake_android):
    import navig_mobile.engine.android.device as adev

    ab = tmp_path / "b.ab"
    ab.write_bytes(b"ANDROID BACKUP")
    seen = {}
    monkeypatch.setattr(adev.shutil, "which", lambda n: "adb")
    monkeypatch.setattr(adev.subprocess, "run",
                        lambda argv, **k: seen.update(argv=argv) or type("R", (), {"returncode": 0})())
    AndroidDevice(fake_android, "ABC123").restore(str(ab))
    assert seen["argv"] == ["adb", "-s", "ABC123", "restore", str(ab)]


def test_android_restore_dir_finds_ab(monkeypatch, tmp_path, fake_android):
    import navig_mobile.engine.android.device as adev
    from navig_mobile.engine.base import DeviceError

    (tmp_path / "x.ab").write_bytes(b"AB")
    seen = {}
    monkeypatch.setattr(adev.shutil, "which", lambda n: "adb")
    monkeypatch.setattr(adev.subprocess, "run",
                        lambda argv, **k: seen.update(argv=argv) or type("R", (), {"returncode": 0})())
    AndroidDevice(fake_android, "A").restore(str(tmp_path))
    assert seen["argv"][-1] == str(tmp_path / "x.ab")
    # missing file raises
    with pytest.raises(DeviceError):
        AndroidDevice(fake_android, "A").restore(str(tmp_path / "none" / "x.ab"))


def test_ios_restore_args():
    seen = {}

    def runner(args, **k):
        seen["args"] = args
        return ""

    IosDevice("I1", runner=runner).restore("/backups", password="pw")
    assert seen["args"] == ["backup2", "restore", "/backups", "--password", "pw", "--udid", "I1"]


# ── backup (must verify success — adb exits 0 even on a declined prompt) ───────

def _fake_adb_run(rc, *, write=None):
    """A ``subprocess.run`` stand-in for ``adb backup``: optionally writes the
    ``-f`` target (adb creates the archive there), then returns a result with *rc*."""
    def _run(argv, **k):
        if write is not None:
            Path(argv[argv.index("-f") + 1]).write_bytes(write)
        return type("R", (), {"returncode": rc})()
    return _run


def test_android_backup_success(monkeypatch, tmp_path, fake_android):
    import navig_mobile.engine.android.device as adev

    monkeypatch.setattr(adev.shutil, "which", lambda n: "adb")
    monkeypatch.setattr(adev.subprocess, "run",
                        _fake_adb_run(0, write=b"ANDROID BACKUP\n5\n0\nnone\n" + b"\x00" * 4096))
    out = AndroidDevice(fake_android, "ABC123").backup(str(tmp_path))
    assert out == str(tmp_path / "ABC123.ab")
    assert Path(out).exists()


def test_android_backup_nonzero_rc_raises(monkeypatch, tmp_path, fake_android):
    import navig_mobile.engine.android.device as adev

    monkeypatch.setattr(adev.shutil, "which", lambda n: "adb")
    monkeypatch.setattr(adev.subprocess, "run", _fake_adb_run(1))
    with pytest.raises(DeviceError, match="exit 1"):
        AndroidDevice(fake_android, "A").backup(str(tmp_path))


def test_android_backup_cancelled_stub_raises(monkeypatch, tmp_path, fake_android):
    # adb exits 0 even when the user declines the on-device prompt, writing only a
    # tiny header — that must NOT be reported as a successful backup.
    import navig_mobile.engine.android.device as adev

    monkeypatch.setattr(adev.shutil, "which", lambda n: "adb")
    monkeypatch.setattr(adev.subprocess, "run",
                        _fake_adb_run(0, write=b"ANDROID BACKUP\n5\n0\nnone\n"))
    with pytest.raises(DeviceError, match="no data"):
        AndroidDevice(fake_android, "A").backup(str(tmp_path))


def test_android_backup_missing_file_raises(monkeypatch, tmp_path, fake_android):
    # rc=0 but adb wrote nothing at all (some versions, on cancel/offline).
    import navig_mobile.engine.android.device as adev

    monkeypatch.setattr(adev.shutil, "which", lambda n: "adb")
    monkeypatch.setattr(adev.subprocess, "run", _fake_adb_run(0))
    with pytest.raises(DeviceError, match="no data"):
        AndroidDevice(fake_android, "A").backup(str(tmp_path))


# ── review regressions (H2, M1) ──────────────────────────────────────────────

def test_android_wraps_raw_errors_as_deviceerror():
    # H2: a mid-op adbutils error (device disconnect) must surface as DeviceError,
    # not leak a raw traceback past the command's `except DeviceError`.
    class BoomRaw:
        def shell(self, cmd):
            raise RuntimeError("adb: device 'A1' not found")

    with pytest.raises(DeviceError):
        AndroidDevice(BoomRaw(), "A1").ls("/sdcard")


def test_android_ls_quotes_spaces():
    # M1: a path with spaces must be quoted for the on-device shell.
    seen = {}

    class Raw:
        def shell(self, cmd):
            seen["cmd"] = cmd
            return ""

    AndroidDevice(Raw(), "A1").ls("/sdcard/My Folder")
    assert "'/sdcard/My Folder'" in seen["cmd"]
    AndroidDevice(Raw(), "A1").ls("/sdcard")
    assert seen["cmd"] == "ls -laL /sdcard"  # simple paths stay unquoted
