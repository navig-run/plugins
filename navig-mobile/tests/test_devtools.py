"""Dev-tools engine tests — frida/objection + iOS diagnostics arg-building."""

from __future__ import annotations

import os

import pytest

from navig_mobile.engine.base import DeviceError
from navig_mobile.engine.devtools import frida as fr
from navig_mobile.engine.devtools import ios_dev


# ── frida ────────────────────────────────────────────────────────────────────

def test_frida_unavailable(monkeypatch):
    monkeypatch.setattr(fr, "_tool", lambda n: None)
    with pytest.raises(fr.FridaUnavailable):
        fr.ps()


def test_frida_available(monkeypatch):
    monkeypatch.setattr(fr, "_tool", lambda n: "frida-ps" if n == "frida-ps" else None)
    assert fr.available() is True


def test_frida_ps_args_with_udid(monkeypatch):
    seen = {}
    monkeypatch.setattr(fr, "_run",
                        lambda tool, args, **k: (seen.update(tool=tool, args=args) or (0, "out")))
    fr.ps("A1", apps=True)
    assert seen["tool"] == "frida-ps"
    assert seen["args"] == ["-D", "A1", "-ai"]


def test_frida_ps_usb_default(monkeypatch):
    seen = {}
    monkeypatch.setattr(fr, "_run", lambda tool, args, **k: (seen.update(args=args) or (0, "")))
    fr.ps(None, apps=False)
    assert seen["args"] == ["-U"]


def test_objection_missing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda n: None)
    with pytest.raises(DeviceError):
        fr.objection("com.foo")


# ── iOS diagnostics ──────────────────────────────────────────────────────────

def test_crash_pull_args_and_makes_dir(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(ios_dev, "_pmd3_run",
                        lambda args, udid, **k: seen.update(args=args, udid=udid) or "")
    dest = tmp_path / "cl"
    out = ios_dev.crash_pull("I1", str(dest))
    assert seen["args"] == ["crash", "pull", str(dest)] and seen["udid"] == "I1"
    assert os.path.isdir(out)


def test_crash_ls_args(monkeypatch):
    seen = {}
    monkeypatch.setattr(ios_dev, "_pmd3_run",
                        lambda args, udid, **k: seen.update(args=args) or "report-1")
    assert ios_dev.crash_ls("I1") == "report-1"
    assert seen["args"] == ["crash", "ls"]


def test_streaming_argv(monkeypatch):
    monkeypatch.setattr(ios_dev, "_cli", lambda: ["py", "-m", "pymobiledevice3"])
    assert ios_dev.syslog_argv("I1") == ["py", "-m", "pymobiledevice3", "syslog", "live", "--udid", "I1"]
    assert ios_dev.pcap_argv("I1", "out.pcap") == \
        ["py", "-m", "pymobiledevice3", "pcap", "out.pcap", "--udid", "I1"]


def test_dvt_argv(monkeypatch):
    monkeypatch.setattr(ios_dev, "_cli", lambda: ["py", "-m", "pymobiledevice3"])
    assert ios_dev.dvt_argv("I1", ["proclist"]) == \
        ["py", "-m", "pymobiledevice3", "developer", "dvt", "proclist", "--udid", "I1"]
