"""CLI wiring tests via Typer's CliRunner — proves command → engine → output for
the MVP verbs, using fake discovery/devices (no physical device)."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from navig_mobile.commands.mobile import mobile_app
from navig_mobile.engine.base import AppInfo, DeviceInfo, Platform

runner = CliRunner()


class FakeDevice:
    platform = Platform.ANDROID
    udid = "A1"

    def info(self):
        return DeviceInfo(udid="A1", platform=Platform.ANDROID, name="Pixel",
                          model="Pixel 8", os_version="15", battery=87,
                          storage_total=100, storage_free=60)

    def apps(self, *, system=False):
        return [AppInfo(app_id="com.foo", name="Foo", version="1.0"),
                AppInfo(app_id="com.bar", name="Bar", version="2.0")]


def _patch(monkeypatch, devices=None):
    import navig_mobile.engine.android.device as adev
    import navig_mobile.engine.ios.device as idev

    infos = devices if devices is not None else [
        DeviceInfo(udid="A1", platform=Platform.ANDROID, name="Pixel", model="Pixel 8",
                   os_version="15")]
    monkeypatch.setattr(adev, "ensure_available", lambda: None)
    monkeypatch.setattr(idev, "ensure_available", lambda: None)
    monkeypatch.setattr(adev, "list_devices", lambda: list(infos))
    monkeypatch.setattr(idev, "list_devices", lambda: [])
    monkeypatch.setattr(adev, "get_device", lambda u: FakeDevice())


def test_doctor_runs():
    result = runner.invoke(mobile_app, ["doctor", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert any(t["key"] == "adbutils" for t in data)


def test_devices_json(monkeypatch):
    _patch(monkeypatch)
    result = runner.invoke(mobile_app, ["devices", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert len(data) == 1 and data[0]["udid"] == "A1"
    assert data[0]["platform"] == "android"


def test_devices_none_connected(monkeypatch):
    _patch(monkeypatch, devices=[])
    result = runner.invoke(mobile_app, ["devices"])
    assert result.exit_code == 0
    assert "No devices connected" in result.stdout


def test_info_json(monkeypatch):
    _patch(monkeypatch)
    result = runner.invoke(mobile_app, ["info", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["name"] == "Pixel" and data["battery"] == 87


def test_apps_list_json(monkeypatch):
    _patch(monkeypatch)
    result = runner.invoke(mobile_app, ["apps", "list", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert {a["app_id"] for a in data} == {"com.foo", "com.bar"}


def test_ambiguous_requires_udid(monkeypatch):
    two = [DeviceInfo(udid="A1", platform=Platform.ANDROID, name="Pixel"),
           DeviceInfo(udid="I1", platform=Platform.IOS, name="iPhone")]
    import navig_mobile.engine.android.device as adev
    import navig_mobile.engine.ios.device as idev

    monkeypatch.setattr(adev, "ensure_available", lambda: None)
    monkeypatch.setattr(idev, "ensure_available", lambda: None)
    monkeypatch.setattr(adev, "list_devices", lambda: [two[0]])
    monkeypatch.setattr(idev, "list_devices", lambda: [two[1]])
    result = runner.invoke(mobile_app, ["info"])
    assert result.exit_code == 2
    assert "--udid" in result.stdout


def test_backup_list_json():
    from navig_mobile.store import get_store

    get_store().record_backup(udid="A1", platform="android", path="/b/A1.ab",
                              encrypted=False, size_bytes=999, note="t")
    result = runner.invoke(mobile_app, ["backup", "list", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert any(b["path"] == "/b/A1.ab" for b in data)


def test_staged_pillar_is_honest():
    # `pair` stays a guidance stub (pairing is a physical/UI action); assert the
    # stable _staged footer rather than a stage number (which changes as pillars ship).
    result = runner.invoke(mobile_app, ["pair"])
    assert result.exit_code == 0
    assert "Available now" in result.stdout


def test_logs_android_recent(monkeypatch):
    import navig_mobile.engine.android.device as adev
    import navig_mobile.engine.ios.device as idev
    from navig_mobile.engine.base import DeviceInfo, Platform

    class FakeRaw:
        def shell(self, cmd):
            return "01-01 12:00:00 I Test: hello" if cmd.startswith("logcat") else ""

    class FakeAndroid:
        platform = Platform.ANDROID
        udid = "A1"
        _raw = FakeRaw()

    monkeypatch.setattr(adev, "ensure_available", lambda: None)
    monkeypatch.setattr(idev, "ensure_available", lambda: None)
    monkeypatch.setattr(adev, "list_devices",
                        lambda: [DeviceInfo(udid="A1", platform=Platform.ANDROID, name="Pixel")])
    monkeypatch.setattr(idev, "list_devices", lambda: [])
    monkeypatch.setattr(adev, "get_device", lambda u: FakeAndroid())
    r = runner.invoke(mobile_app, ["logs", "-u", "A1", "-n", "50"])
    assert r.exit_code == 0 and "hello" in r.stdout


def test_help_lists_all_pillars():
    result = runner.invoke(mobile_app, ["--help"])
    assert result.exit_code == 0
    for panel in ("Device & Connect", "Apps", "Backup & Restore",
                  "Forensics & Investigate", "Spyware Scan", "Rooting & Jailbreak"):
        assert panel in result.stdout


# ── Stage 2: consent gating ──────────────────────────────────────────────────

def test_consent_record_then_show(monkeypatch):
    _patch(monkeypatch)
    r = runner.invoke(mobile_app, ["consent", "record", "-u", "A1",
                                   "--authorization", "I own this device", "--scope", "all"])
    assert r.exit_code == 0, r.stdout
    r2 = runner.invoke(mobile_app, ["consent", "show", "-u", "A1", "--json"])
    assert r2.exit_code == 0
    assert json.loads(r2.stdout)["authorization_ref"] == "I own this device"


def test_consent_record_empty_authorization_refused(monkeypatch):
    _patch(monkeypatch)
    r = runner.invoke(mobile_app, ["consent", "record", "-u", "A1", "--authorization", ""])
    assert r.exit_code == 2
    assert "authorization" in r.stdout.lower()


def test_forensics_acquire_blocked_without_consent(monkeypatch):
    _patch(monkeypatch)
    r = runner.invoke(mobile_app, ["forensics", "acquire", "-u", "A1"])
    assert r.exit_code == 4
    assert "authorization" in r.stdout.lower()


def test_scan_spyware_blocked_without_consent(monkeypatch):
    _patch(monkeypatch)
    r = runner.invoke(mobile_app, ["scan", "spyware", "-u", "A1"])
    assert r.exit_code == 4


def test_media_pull_blocked_without_consent(monkeypatch):
    _patch(monkeypatch)
    r = runner.invoke(mobile_app, ["media", "-u", "A1"])
    assert r.exit_code == 4


def test_case_list_empty():
    r = runner.invoke(mobile_app, ["case", "list"])
    assert r.exit_code == 0
    assert "No cases" in r.stdout


def test_scan_spyware_missing_mvt_after_consent(monkeypatch):
    _patch(monkeypatch)
    # grant consent so we get past the gate, then MVT is absent → guided exit
    from navig_mobile.consent import ConsentGate
    ConsentGate().record(udid="A1", authorization_ref="I own this device")
    import navig_mobile.engine.forensics.spyware as sp
    monkeypatch.setattr(sp, "find_mvt", lambda p: None)
    r = runner.invoke(mobile_app, ["scan", "spyware", "-u", "A1"])
    assert r.exit_code == 3
    assert "MVT" in r.stdout


# ── Stage 3: rooting / jailbreak ─────────────────────────────────────────────

def test_root_status_json(monkeypatch):
    _patch(monkeypatch)
    import navig_mobile.engine.rooting.android as aroot
    monkeypatch.setattr(aroot, "root_status",
                        lambda dev: {"platform": "android", "rooted": False,
                                     "bootloader_locked": True, "selinux": "Enforcing"})
    r = runner.invoke(mobile_app, ["root", "status", "-u", "A1", "--json"])
    assert r.exit_code == 0, r.stdout
    assert json.loads(r.stdout)["rooted"] is False


def test_root_and_jailbreak_guides():
    for cmd, needle in ([["root", "guide"], "Magisk"], [["jailbreak", "guide"], "checkm8"]):
        r = runner.invoke(mobile_app, cmd)
        assert r.exit_code == 0 and needle in r.stdout


def test_root_unlock_double_gate(monkeypatch):
    _patch(monkeypatch)
    import navig_mobile.engine.rooting.android as aroot
    monkeypatch.setattr(aroot, "find_fastboot", lambda: "fastboot")
    monkeypatch.setattr(aroot, "fastboot_devices", lambda: ["A1"])
    unlocked = {"called": False}
    monkeypatch.setattr(aroot, "unlock",
                        lambda serial=None: (unlocked.update(called=True) or (0, "OKAY")))

    # gate 1: no consent → refused (exit 4), unlock NOT called
    r = runner.invoke(mobile_app, ["root", "unlock"])
    assert r.exit_code == 4 and unlocked["called"] is False

    # record consent → gate 2: no --yes → refuses, unlock NOT called
    from navig_mobile.consent import ConsentGate
    ConsentGate().record(udid="A1", authorization_ref="I own this device")
    r = runner.invoke(mobile_app, ["root", "unlock"])
    assert r.exit_code == 0 and unlocked["called"] is False
    assert "Refusing" in r.stdout or "DESTRUCTIVE" in r.stdout

    # both gates satisfied → unlock runs
    r = runner.invoke(mobile_app, ["root", "unlock", "--yes"])
    assert unlocked["called"] is True


def test_root_unlock_no_fastboot_device(monkeypatch):
    _patch(monkeypatch)
    import navig_mobile.engine.rooting.android as aroot
    monkeypatch.setattr(aroot, "find_fastboot", lambda: "fastboot")
    monkeypatch.setattr(aroot, "fastboot_devices", lambda: [])
    r = runner.invoke(mobile_app, ["root", "unlock", "--yes"])
    assert r.exit_code == 1
    assert "fastboot mode" in r.stdout


def test_dev_enable_android_guidance(monkeypatch):
    _patch(monkeypatch)
    r = runner.invoke(mobile_app, ["dev", "enable", "-u", "A1"])
    assert r.exit_code == 0
    assert "Developer Options" in r.stdout


def test_android_fastboot_missing_binary(monkeypatch):
    import navig_mobile.engine.rooting.android as aroot
    monkeypatch.setattr(aroot, "find_fastboot", lambda: None)
    r = runner.invoke(mobile_app, ["android", "fastboot", "devices"])
    assert r.exit_code == 127


# ── Stage 4: dev-tools + OSINT ───────────────────────────────────────────────

def test_osint_device_json(monkeypatch):
    _patch(monkeypatch)
    import navig_mobile.engine.osint.report as osint
    monkeypatch.setattr(osint, "device_identity",
                        lambda dev: {"platform": "android", "model": "Pixel", "serial": "S1"})
    r = runner.invoke(mobile_app, ["osint", "device", "-u", "A1", "--json"])
    assert r.exit_code == 0
    assert json.loads(r.stdout)["model"] == "Pixel"


def test_osint_apps_scan_json(monkeypatch):
    _patch(monkeypatch)
    import navig_mobile.engine.osint.report as osint
    monkeypatch.setattr(osint, "app_privacy_scan",
                        lambda dev, limit=40: {"platform": "android", "scanned": 2, "flagged": 1,
                                               "truncated": False, "total_user_apps": 2,
                                               "apps": [{"app": "com.x", "name": "X",
                                                         "risk_score": 3, "grants": ["camera"]}]})
    r = runner.invoke(mobile_app, ["osint", "apps", "-u", "A1", "--json"])
    assert r.exit_code == 0 and json.loads(r.stdout)["flagged"] == 1


def test_osint_report_saves(monkeypatch, tmp_path):
    _patch(monkeypatch)
    import navig_mobile.engine.osint.report as osint
    monkeypatch.setattr(osint, "build_report",
                        lambda dev, limit=40: {"identity": {"platform": "android"},
                                               "app_privacy": {"apps": [], "scanned": 0},
                                               "handoff": "war-room", "udid": "A1"})
    monkeypatch.setattr(osint, "write_report", lambda rep, out=None: str(tmp_path / "r.json"))
    r = runner.invoke(mobile_app, ["osint", "report", "-u", "A1"])
    assert r.exit_code == 0 and "saved" in r.stdout.lower()


def test_dev_frida_missing(monkeypatch):
    _patch(monkeypatch)
    import navig_mobile.engine.devtools.frida as fr
    monkeypatch.setattr(fr, "_tool", lambda n: None)
    r = runner.invoke(mobile_app, ["dev", "frida"])
    assert r.exit_code == 127 and "frida" in r.stdout.lower()


def test_dev_attach_needs_target(monkeypatch):
    _patch(monkeypatch)
    r = runner.invoke(mobile_app, ["dev", "attach"])
    assert r.exit_code == 2  # _need(target)


def test_ios_crashlogs_wired(monkeypatch, tmp_path):
    import navig_mobile.engine.android.device as adev
    import navig_mobile.engine.devtools.ios_dev as iod
    import navig_mobile.engine.ios.device as idev
    from navig_mobile.engine.base import DeviceInfo, Platform

    monkeypatch.setattr(adev, "ensure_available", lambda: None)
    monkeypatch.setattr(idev, "ensure_available", lambda: None)
    monkeypatch.setattr(adev, "list_devices", lambda: [])
    monkeypatch.setattr(idev, "list_devices",
                        lambda: [DeviceInfo(udid="I1", platform=Platform.IOS, name="iPhone")])

    class FakeIos:
        platform = Platform.IOS
        udid = "I1"

    monkeypatch.setattr(idev, "get_device", lambda u: FakeIos())
    monkeypatch.setattr(iod, "crash_pull", lambda udid, out: out)
    r = runner.invoke(mobile_app, ["ios", "crashlogs", "--out", str(tmp_path / "cl")])
    assert r.exit_code == 0 and "Crash reports" in r.stdout


# ── finish-stubs: restore · pair · instruments ───────────────────────────────

def test_restore_double_gate(monkeypatch, tmp_path):
    _patch(monkeypatch)
    backup = tmp_path / "b.ab"
    backup.write_bytes(b"AB")
    restored = {"called": False}

    class FakeDev:
        platform = Platform.ANDROID
        udid = "A1"

        def restore(self, src, *, password=None, progress=None):
            restored["called"] = True

    import navig_mobile.engine.android.device as adev
    monkeypatch.setattr(adev, "get_device", lambda u: FakeDev())

    # gate 1: no consent → exit 4, restore NOT called
    r = runner.invoke(mobile_app, ["restore", str(backup)])
    assert r.exit_code == 4 and restored["called"] is False

    # gate 2: consent, no --yes → refuses
    from navig_mobile.consent import ConsentGate
    ConsentGate().record(udid="A1", authorization_ref="I own this device")
    r = runner.invoke(mobile_app, ["restore", str(backup)])
    assert r.exit_code == 0 and restored["called"] is False
    assert "Refusing" in r.stdout or "DESTRUCTIVE" in r.stdout

    # both gates → restore runs
    r = runner.invoke(mobile_app, ["restore", str(backup), "--yes"])
    assert restored["called"] is True


def test_restore_missing_path():
    r = runner.invoke(mobile_app, ["restore", "/does/not/exist.ab"])
    assert r.exit_code == 2


def test_pair_guidance_no_arg():
    r = runner.invoke(mobile_app, ["pair"])
    assert r.exit_code == 0 and "Available now" in r.stdout


def test_pair_wireless_no_adb(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda n: None)
    r = runner.invoke(mobile_app, ["pair", "192.168.1.5:37000", "--code", "123456"])
    assert r.exit_code == 127


def test_ios_instruments_usage(monkeypatch):
    import navig_mobile.engine.android.device as adev
    import navig_mobile.engine.ios.device as idev
    from navig_mobile.engine.base import DeviceInfo, Platform

    monkeypatch.setattr(adev, "ensure_available", lambda: None)
    monkeypatch.setattr(idev, "ensure_available", lambda: None)
    monkeypatch.setattr(adev, "list_devices", lambda: [])
    monkeypatch.setattr(idev, "list_devices",
                        lambda: [DeviceInfo(udid="I1", platform=Platform.IOS, name="iPhone")])

    class FakeIos:
        platform = Platform.IOS
        udid = "I1"

    monkeypatch.setattr(idev, "get_device", lambda u: FakeIos())
    r = runner.invoke(mobile_app, ["ios", "instruments"])
    assert r.exit_code == 0 and "Usage" in r.stdout


# ── review regressions (H1, M2) ──────────────────────────────────────────────

def test_run_with_spinner_runs_once_and_propagates():
    # H1: the wrapped op must run EXACTLY once and its exception must propagate
    # (old code swallowed it and re-ran fn → double backup/acquire/flash).
    from navig_mobile.commands.mobile import _run_with_spinner

    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise RuntimeError("boom")

    raised = False
    try:
        _run_with_spinner(fn, "working")
    except RuntimeError:
        raised = True
    assert raised and calls["n"] == 1


def test_scan_iocs_missing_mvt_exits_3(monkeypatch, tmp_path):
    # M2: MVT-missing must exit 3 consistently across all scan actions.
    import navig_mobile.engine.forensics.spyware as sp
    monkeypatch.setattr(sp, "find_mvt", lambda p: None)
    results = tmp_path / "results"
    results.mkdir()
    feed = tmp_path / "feed.stix2"
    feed.write_text("{}", encoding="utf-8")
    r = runner.invoke(mobile_app, ["scan", "iocs", str(results),
                                   "--platform", "ios", "--iocs", str(feed)])
    assert r.exit_code == 3
