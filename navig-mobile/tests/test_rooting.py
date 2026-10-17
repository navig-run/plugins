"""Rooting/jailbreak engine tests — root detection, checkm8 eligibility, and
fastboot / pymobiledevice3 driver arg-building (all subprocess-mocked)."""

from __future__ import annotations

import pytest

from navig_mobile.engine.base import DeviceError, DeviceInfo, Platform
from navig_mobile.engine.rooting import android as aroot
from navig_mobile.engine.rooting import ios as jb


class FakeRaw:
    def __init__(self, props=None, shell_map=None):
        self._props = props or {}
        self._shell = shell_map or {}

    def getprop(self, name):
        return self._props.get(name, "")

    def shell(self, cmd):
        return self._shell.get(cmd, "")


class FakeDev:
    def __init__(self, raw):
        self._raw = raw


# ── Android root status ──────────────────────────────────────────────────────

def test_root_status_rooted_unlocked():
    raw = FakeRaw(
        props={"ro.boot.flash.locked": "0", "ro.boot.verifiedbootstate": "orange",
               "ro.build.tags": "release-keys", "ro.boot.veritymode": "disabled"},
        shell_map={"which su": "/system/bin/su", "getenforce": "Permissive",
                   "pm list packages com.topjohnwu.magisk": "package:com.topjohnwu.magisk"},
    )
    st = aroot.root_status(FakeDev(raw))
    assert st["rooted"] is True
    assert st["su_path"] == "/system/bin/su"
    assert st["magisk_app"] is True
    assert st["bootloader_locked"] is False
    assert st["selinux"] == "Permissive"


def test_root_status_stock_locked():
    raw = FakeRaw(
        props={"ro.boot.flash.locked": "1", "ro.boot.verifiedbootstate": "green",
               "ro.build.tags": "release-keys"},
        shell_map={"getenforce": "Enforcing"},
    )
    st = aroot.root_status(FakeDev(raw))
    assert st["rooted"] is False
    assert st["bootloader_locked"] is True
    assert st["selinux"] == "Enforcing"


def test_root_status_bootloader_from_verifiedboot_when_no_flag():
    raw = FakeRaw(props={"ro.boot.verifiedbootstate": "green"})
    st = aroot.root_status(FakeDev(raw))
    assert st["bootloader_locked"] is True  # inferred from green verified boot


def test_root_status_non_android_raises():
    with pytest.raises(DeviceError):
        aroot.root_status(object())


# ── fastboot drivers ─────────────────────────────────────────────────────────

def test_unlock_builds_args(monkeypatch):
    seen = {}
    monkeypatch.setattr(aroot, "_run_fastboot",
                        lambda args, **k: (seen.update(args=args) or (0, "OKAY", "")))
    rc, out = aroot.unlock("A1")
    assert seen["args"] == ["-s", "A1", "flashing", "unlock"] and rc == 0


def test_flash_builds_args(monkeypatch):
    seen = {}
    monkeypatch.setattr(aroot, "_run_fastboot",
                        lambda args, **k: (seen.update(args=args) or (0, "OKAY", "")))
    aroot.flash("boot", "patched.img", "A1")
    assert seen["args"] == ["-s", "A1", "flash", "boot", "patched.img"]


def test_fastboot_missing_raises(monkeypatch):
    monkeypatch.setattr(aroot, "find_fastboot", lambda: None)
    with pytest.raises(DeviceError):
        aroot.unlock("A1")


def test_fastboot_devices_empty_when_absent(monkeypatch):
    monkeypatch.setattr(aroot, "find_fastboot", lambda: None)
    assert aroot.fastboot_devices() == []


# ── iOS checkm8 eligibility ──────────────────────────────────────────────────

@pytest.mark.parametrize("product,expected", [
    ("iPhone10,3", True),    # iPhone X (A11)
    ("iPhone9,1", True),     # iPhone 7 (A10)
    ("iPhone11,2", False),   # iPhone XS (A12)
    ("iPhone14,5", False),   # iPhone 13 (A15)
    ("iPad7,5", True),       # A10
    ("iPad8,1", False),      # A12X
    ("", None),
    ("Watch6,1", None),
])
def test_checkm8_eligibility(product, expected):
    assert jb.checkm8_eligible(product)[0] is expected


def test_jailbreak_status(monkeypatch):
    monkeypatch.setattr(jb, "developer_mode_status", lambda u: True)

    class FakeIos:
        udid = "I1"

        def info(self):
            return DeviceInfo(udid="I1", platform=Platform.IOS,
                              product_type="iPhone10,3", os_version="16.1")

    st = jb.jailbreak_status(FakeIos())
    assert st["checkm8_eligible"] is True
    assert st["developer_mode"] is True
    assert "palera1n" in st["recommended_tool"]


def test_ios_recovery_and_devmode_args(monkeypatch):
    seen = {}
    monkeypatch.setattr(jb, "_pmd3",
                        lambda args, udid, **k: (seen.update(args=args, udid=udid) or "OK"))
    jb.enter_recovery("I1")
    assert seen["args"] == ["restore", "enter"] and seen["udid"] == "I1"
    jb.exit_recovery("I1")
    assert seen["args"] == ["restore", "exit"]
    jb.enable_developer_mode("I1")
    assert seen["args"] == ["amfi", "enable-developer-mode"]


def test_developer_mode_status_parsing(monkeypatch):
    monkeypatch.setattr(jb, "_pmd3", lambda args, udid, **k: "Developer mode is enabled: true")
    assert jb.developer_mode_status("I1") is True
    monkeypatch.setattr(jb, "_pmd3", lambda args, udid, **k: "disabled")
    assert jb.developer_mode_status("I1") is False
    def boom(*a, **k):
        raise DeviceError("no device")
    monkeypatch.setattr(jb, "_pmd3", boom)
    assert jb.developer_mode_status("I1") is None
