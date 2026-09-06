"""OSINT engine tests — device identity + Android app privacy report."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from navig_mobile.engine.base import AppInfo, DeviceError, Platform
from navig_mobile.engine.osint import report as osint


class FakeRaw:
    def __init__(self, props=None, shell_map=None, dumpsys=None):
        self._props = props or {}
        self._shell = shell_map or {}
        self._dumpsys = dumpsys or {}

    def getprop(self, name):
        return self._props.get(name, "")

    def shell(self, cmd):
        if cmd.startswith("dumpsys package "):
            return self._dumpsys.get(cmd.split()[-1], "")
        return self._shell.get(cmd, "")


class FakeAndroidDev:
    platform = Platform.ANDROID
    udid = "A1"

    def __init__(self, raw, apps=None):
        self._raw = raw
        self._apps = apps or []

    def apps(self, *, system=False):
        return self._apps


class FakeIos:
    platform = Platform.IOS
    udid = "I1"

    def lockdown_values(self):
        return {"DeviceName": "iPhone", "SerialNumber": "F2LABC",
                "InternationalMobileEquipmentIdentity": "35566...", "WiFiAddress": "aa:bb:cc",
                "UniqueChipID": 123, "SomeIrrelevantKey": "x"}

    def apps(self, *, system=False):
        return []


# ── identity ─────────────────────────────────────────────────────────────────

def test_android_identity():
    raw = FakeRaw(
        props={"ro.product.model": "Pixel 8", "ro.serialno": "SER1",
               "ro.build.version.release": "15", "ro.product.manufacturer": "Google"},
        shell_map={"settings get secure android_id": "abc123def"},
    )
    ident = osint.device_identity(FakeAndroidDev(raw))
    assert ident["platform"] == "android"
    assert ident["model"] == "Pixel 8" and ident["serial"] == "SER1"
    assert ident["android_id"] == "abc123def"


def test_android_identity_null_android_id():
    raw = FakeRaw(shell_map={"settings get secure android_id": "null"})
    assert osint.device_identity(FakeAndroidDev(raw))["android_id"] is None


def test_android_identity_non_android_raises():
    class NoRaw:
        platform = Platform.ANDROID

    with pytest.raises(DeviceError):
        osint._android_identity(NoRaw())


def test_ios_identity_filters_relevant_keys():
    ident = osint.device_identity(FakeIos())
    assert ident["platform"] == "ios"
    assert ident["SerialNumber"] == "F2LABC"
    assert "InternationalMobileEquipmentIdentity" in ident
    assert "SomeIrrelevantKey" not in ident


# ── app permissions / privacy ────────────────────────────────────────────────

def test_app_permissions_flags_dangerous():
    dumpsys = ("com.foo:\n  requested permissions:\n"
               "    android.permission.INTERNET\n"
               "    android.permission.CAMERA\n"
               "    android.permission.ACCESS_FINE_LOCATION\n")
    dev = FakeAndroidDev(FakeRaw(dumpsys={"com.foo": dumpsys}))
    prof = osint.app_permissions(dev, "com.foo")
    assert prof["risk_score"] == 2
    assert set(prof["dangerous"].values()) == {"camera", "precise location"}


def test_app_permissions_ios_note():
    prof = osint.app_permissions(FakeIos(), "com.foo")
    assert prof["platform"] == "ios" and "note" in prof


def test_app_privacy_scan_ranks_by_risk():
    d_risky = ("android.permission.CAMERA android.permission.RECORD_AUDIO "
               "android.permission.READ_SMS")
    d_safe = "android.permission.INTERNET"
    raw = FakeRaw(dumpsys={"com.risky": d_risky, "com.safe": d_safe})
    dev = FakeAndroidDev(raw, apps=[AppInfo(app_id="com.safe", name="Safe"),
                                    AppInfo(app_id="com.risky", name="Risky")])
    scan = osint.app_privacy_scan(dev, limit=10)
    assert scan["flagged"] == 1
    assert scan["apps"][0]["app"] == "com.risky"
    assert scan["apps"][0]["risk_score"] == 3


def test_app_privacy_scan_truncates():
    raw = FakeRaw(dumpsys={f"com.a{i}": "android.permission.CAMERA" for i in range(5)})
    apps = [AppInfo(app_id=f"com.a{i}", name=f"A{i}") for i in range(5)]
    scan = osint.app_privacy_scan(FakeAndroidDev(raw, apps=apps), limit=2)
    assert scan["scanned"] == 2 and scan["truncated"] is True


def test_app_privacy_scan_ios_note():
    scan = osint.app_privacy_scan(FakeIos())
    assert scan["platform"] == "ios" and scan["scanned"] == 0


def test_build_and_write_report(tmp_path):
    raw = FakeRaw(props={"ro.product.model": "Pixel"},
                  shell_map={"settings get secure android_id": "x"})
    rep = osint.build_report(FakeAndroidDev(raw, apps=[]), limit=5)
    assert rep["udid"] == "A1" and "identity" in rep and "app_privacy" in rep
    path = osint.write_report(rep, str(tmp_path / "r.json"))
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["udid"] == "A1" and data["identity"]["model"] == "Pixel"
