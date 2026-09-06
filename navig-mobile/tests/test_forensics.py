"""Forensics engine tests — acquisition, MVT spyware scan, LEAPP parse.

All external tools are subprocess-mocked; no MVT/LEAPP/device required."""

from __future__ import annotations

from pathlib import Path

import pytest
import typer

from navig_mobile.consent import CaseDir
from navig_mobile.engine.base import Platform
from navig_mobile.engine.forensics import acquire, leapp, spyware


class FakeIosDev:
    platform = Platform.IOS
    udid = "I1"

    def backup(self, dest, *, encrypted=False, progress=None):
        Path(dest).mkdir(parents=True, exist_ok=True)
        (Path(dest) / "Manifest.plist").write_bytes(b"plist")
        return dest


class FakeAndroidDev:
    platform = Platform.ANDROID
    udid = "A1"

    class _Raw:
        def shell(self, cmd):
            return "prop=1" if "getprop" in cmd else "package:com.x"

    _raw = _Raw()


# ── acquisition ──────────────────────────────────────────────────────────────

def test_acquire_ios_produces_backup(tmp_path):
    cd = CaseDir.create(udid="I1", platform="ios", base=tmp_path)
    summary = acquire.acquire(FakeIosDev(), cd)
    assert summary["type"] == "backup"
    assert summary["mvt_command"] == "check-backup"
    assert cd.manifest["artifacts"], "backup should be recorded as evidence"


def test_acquire_android_produces_bugreport(tmp_path, monkeypatch):
    def fake_adb(args, *, timeout=1800):
        Path(args[-1]).write_bytes(b"PK\x03\x04fake-zip")
        return 0

    monkeypatch.setattr(acquire, "_run_adb", fake_adb)
    cd = CaseDir.create(udid="A1", platform="android", base=tmp_path)
    summary = acquire.acquire(FakeAndroidDev(), cd)
    assert summary["type"] == "bugreport"
    assert summary["mvt_command"] == "check-bugreport"
    paths = [a["path"] for a in cd.manifest["artifacts"]]
    assert any("bugreport" in p for p in paths)
    assert any("getprop" in p for p in paths)  # snapshot recorded too


# ── spyware (MVT) ────────────────────────────────────────────────────────────

def test_spyware_scan_builds_correct_args(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(spyware, "find_mvt",
                        lambda p: "mvt-ios" if p == "ios" else "mvt-android")

    def fake_run(argv, *, timeout=1800):
        seen["argv"] = argv
        return 0, "", ""

    monkeypatch.setattr(spyware, "_run_mvt", fake_run)

    res = spyware.scan("ios", str(tmp_path / "backup"), str(tmp_path / "out"))
    assert "check-backup" in seen["argv"] and "--output" in seen["argv"]
    assert res["clean"] is True

    spyware.scan("android", str(tmp_path / "br.zip"), str(tmp_path / "out2"))
    assert "check-bugreport" in seen["argv"]  # NOT the removed check-adb


def test_spyware_counts_detections(tmp_path, monkeypatch):
    monkeypatch.setattr(spyware, "find_mvt", lambda p: "mvt-ios")
    out = tmp_path / "out"
    out.mkdir()
    (out / "sms_detected.json").write_text('[{"a":1},{"b":2}]', encoding="utf-8")
    monkeypatch.setattr(spyware, "_run_mvt", lambda argv, *, timeout=1800: (0, "", ""))
    res = spyware.scan("ios", str(tmp_path / "b"), str(out))
    assert res["detections"] == 2 and res["clean"] is False


def test_spyware_unavailable_is_guided(tmp_path, monkeypatch):
    monkeypatch.setattr(spyware, "find_mvt", lambda p: None)
    with pytest.raises(spyware.MvtUnavailable):
        spyware.scan("ios", str(tmp_path), str(tmp_path / "o"))


# ── the scan VERDICT reaches the shell ───────────────────────────────────────
#
# `navig mobile scan spyware --udid X && echo "device clean"` printed *device clean*
# on a phone MVT had just flagged: every outcome was reported on screen and returned
# 0. On a spyware surface the exit code is the whole point — the caller is a script.


def _report(res, *, json_out=False):
    """Drive the CLI's reporter directly; it owns the verdict."""
    from navig_mobile.commands.mobile import _report_scan

    _report_scan(res, None, json_out)


def _res(**over):
    base = {"tool": "mvt-ios check-backup", "output": "/tmp/out", "returncode": 0,
            "detections": 0, "clean": True, "stderr_tail": "", "caveat": "not proof"}
    return {**base, **over}


@pytest.mark.parametrize("json_out", [False, True])
def test_detections_exit_non_zero(json_out, capsys):
    with pytest.raises(typer.Exit) as excinfo:
        _report(_res(detections=2, clean=False), json_out=json_out)
    assert excinfo.value.exit_code == 1
    # The detail still prints — the code is the verdict, not a replacement for it.
    assert capsys.readouterr().out.strip()


@pytest.mark.parametrize("json_out", [False, True])
def test_a_clean_scan_exits_zero(json_out):
    _report(_res(), json_out=json_out)          # no raise


def test_mvt_failing_without_detections_is_not_clean(capsys):
    """"I could not look" must not read as "you are fine"."""
    with pytest.raises(typer.Exit) as excinfo:
        _report(_res(returncode=2, clean=False, stderr_tail="mvt exploded"))
    assert excinfo.value.exit_code == 1
    assert "MVT finished" in capsys.readouterr().out


# ── LEAPP ────────────────────────────────────────────────────────────────────

def test_leapp_builds_args_and_infers_type(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(leapp, "find_leapp",
                        lambda p: ["ileapp"] if p == "ios" else ["aleapp"])
    monkeypatch.setattr(leapp, "_run_leapp",
                        lambda argv, *, timeout=1800: (seen.update(argv=argv) or (0, "", "")))
    (tmp_path / "in").mkdir()
    leapp.parse("ios", str(tmp_path / "in"), str(tmp_path / "out"))
    argv = seen["argv"]
    assert argv[0] == "ileapp"
    assert "-t" in argv and "-i" in argv and "-o" in argv
    assert "itunes" in argv  # an iOS backup dir defaults to the itunes input type


def test_leapp_unavailable_is_guided(tmp_path, monkeypatch):
    monkeypatch.setattr(leapp, "find_leapp", lambda p: None)
    with pytest.raises(leapp.LeappUnavailable):
        leapp.parse("android", str(tmp_path), str(tmp_path / "o"))
