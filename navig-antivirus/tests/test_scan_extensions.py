"""scan_extensions must never report a green "clean" over data it couldn't read.

`_read_json` swallowed every error and returned `{}`, so a profile whose
`Secure Preferences` was locked (the browser is running), permission-denied, or
corrupt silently contributed zero extensions — indistinguishable from "genuinely
no extensions". A user with malware-laden extensions then saw "No extensions … ✓".

These tests pin the honest contract: unreadable profiles are counted and surfaced,
a *missing* store is not miscounted as unreadable, and an all-unreadable scan is
refused a clean verdict (non-zero exit, no "clean" line).
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from navig_antivirus.commands.antivirus import antivirus_app
from navig_antivirus.engine import browsers

_KNOWN_BAD = "edkbpkanapinjifakjogefooogoclehg"  # in browsers._KNOWN_BAD → score 20


def _ud(tmp_path: Path) -> Path:
    ud = tmp_path / "User Data"
    ud.mkdir()
    return ud


def _profile(ud: Path, name: str, *, secure_prefs: str | None) -> None:
    d = ud / name
    d.mkdir()
    (d / "Preferences").write_text(json.dumps({"profile": {"name": name}}), encoding="utf-8")
    if secure_prefs is not None:
        (d / "Secure Preferences").write_text(secure_prefs, encoding="utf-8")


def _ext_store(*exts: dict) -> str:
    settings = {
        e["id"]: {
            "manifest": e.get("manifest", {"name": e["id"], "manifest_version": 3}),
            "location": e.get("location", 1),
            "from_webstore": e.get("from_webstore", True),
        }
        for e in exts
    }
    return json.dumps({"extensions": {"settings": settings}})


def test_flags_known_malware(tmp_path):
    ud = _ud(tmp_path)
    _profile(ud, "Default", secure_prefs=_ext_store(
        {"id": _KNOWN_BAD, "manifest": {"name": "saveVPN", "manifest_version": 3},
         "from_webstore": False}))
    findings, total, unique, nprof, unreadable = browsers.scan_extensions(ud, min_score=3)
    assert unreadable == 0
    assert total == 1
    assert findings and findings[0].ext_id == _KNOWN_BAD
    assert findings[0].band == "CRITICAL"


def test_unreadable_profile_is_counted_not_silently_empty(tmp_path):
    """A corrupt/locked Secure Preferences is counted — the whole point of the fix."""
    ud = _ud(tmp_path)
    _profile(ud, "Default", secure_prefs="}{ not json — locked or corrupt")
    findings, total, unique, nprof, unreadable = browsers.scan_extensions(ud, min_score=3)
    assert unreadable == 1
    assert total == 0
    assert findings == []  # nothing read, but the caller now knows it's incomplete


def test_missing_secure_prefs_is_not_unreadable(tmp_path):
    """A profile with no extension store is legitimately empty — must NOT cry wolf."""
    ud = _ud(tmp_path)
    _profile(ud, "Default", secure_prefs=None)
    findings, total, unique, nprof, unreadable = browsers.scan_extensions(ud, min_score=3)
    assert unreadable == 0
    assert total == 0
    assert nprof == 1


def test_partial_read_keeps_findings_and_flags_the_gap(tmp_path):
    """A readable profile's findings still surface, and the unreadable one is flagged
    (so a real threat in a readable profile isn't lost, and coverage gaps are honest)."""
    ud = _ud(tmp_path)
    _profile(ud, "Default", secure_prefs=_ext_store({"id": _KNOWN_BAD, "from_webstore": False}))
    _profile(ud, "Profile 1", secure_prefs="corrupt {{{")
    findings, total, unique, nprof, unreadable = browsers.scan_extensions(ud, min_score=3)
    assert unreadable == 1
    assert len(findings) == 1
    assert nprof == 2


def test_command_refuses_a_clean_verdict_when_all_unreadable(tmp_path):
    """The phantom-clean regression: an all-unreadable scan must exit non-zero and never
    print the green 'No extensions … ✓' line."""
    ud = _ud(tmp_path)
    _profile(ud, "Default", secure_prefs="}{ corrupt")
    result = CliRunner().invoke(antivirus_app, ["extensions", "--user-data", str(ud)])
    assert result.exit_code == 1
    assert "No extensions at or above" not in result.output


def test_command_reports_clean_only_when_genuinely_empty(tmp_path):
    """A profile that is truly empty (readable store, no extensions) is a real clean."""
    ud = _ud(tmp_path)
    _profile(ud, "Default", secure_prefs=json.dumps({"extensions": {"settings": {}}}))
    result = CliRunner().invoke(antivirus_app, ["extensions", "--user-data", str(ud)])
    assert result.exit_code == 0
    assert "No extensions at or above" in result.output
