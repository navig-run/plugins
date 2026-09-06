"""registry_scan must never report a green "clean" over registry it couldn't read.

`_subkeys` / `_read_values` swallowed EVERY OSError to []/{}, so a permission-denied
HKLM root (exactly where enterprise + malware force-installs live, and exactly what a
non-elevated run can't open) looked identical to a genuinely-absent key. `navig
antivirus registry` then printed "No registry-forced Chrome extensions ✓" — a
false-clean over an audit that never happened (the scan_extensions class, #671).

These tests pin the honest contract: a *denied* key is recorded as unreadable, a
*missing* key is NOT (no crying wolf), findings still surface, and the command refuses
a clean verdict when nothing was found but a location was unreadable.

A fake ``winreg`` is injected and ``_WIN`` forced on, so the tests are deterministic
and platform-independent (no real registry access).
"""

from __future__ import annotations

import sys
import types

from typer.testing import CliRunner

from navig_antivirus.commands.antivirus import antivirus_app
from navig_antivirus.engine import registry_scan

_ABSENT = object()


def _install_fake_winreg(monkeypatch, layout: dict):
    """Inject a fake ``winreg``. *layout* maps ``"<HIVE>\\<subkey>"`` to either a list
    of subkey names, a dict of values, or the string ``"DENIED"``. Any path not in
    *layout* raises ``FileNotFoundError`` (i.e. the key is absent)."""
    fake = types.ModuleType("winreg")
    fake.HKEY_CURRENT_USER = "HKCU"
    fake.HKEY_LOCAL_MACHINE = "HKLM"

    class _Key:
        def __init__(self, entry):
            self.entry = entry

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _open_key(root, subkey):
        entry = layout.get(f"{root}\\{subkey}", _ABSENT)
        if entry is _ABSENT:
            raise FileNotFoundError(2, "The system cannot find the file specified")
        if entry == "DENIED":
            raise PermissionError(5, "Access is denied")
        return _Key(entry)

    def _enum_key(key, i):
        names = key.entry if isinstance(key.entry, list) else []
        if i >= len(names):
            raise OSError(259, "No more data is available")
        return names[i]

    def _enum_value(key, i):
        items = list(key.entry.items()) if isinstance(key.entry, dict) else []
        if i >= len(items):
            raise OSError(259, "No more data is available")
        name, val = items[i]
        return (name, val, 1)  # (name, value, type)

    fake.OpenKey = _open_key
    fake.EnumKey = _enum_key
    fake.EnumValue = _enum_value
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(registry_scan, "_WIN", True)


# ── engine: absent vs denied ──────────────────────────────────────────────────


def test_denied_root_is_recorded_as_unreadable(monkeypatch):
    """A permission-denied force root is a coverage gap, not evidence of 'clean'."""
    _install_fake_winreg(
        monkeypatch, {r"HKLM\SOFTWARE\Google\Chrome\Extensions": "DENIED"}
    )
    rep = registry_scan.scan_registry()
    assert rep.available is True
    assert rep.force_installed == []
    assert any("HKLM" in loc and "Extensions" in loc for loc in rep.unreadable)


def test_absent_keys_are_not_unreadable(monkeypatch):
    """A machine with none of the keys is genuinely clean — must NOT cry wolf."""
    _install_fake_winreg(monkeypatch, {})  # every OpenKey → FileNotFoundError
    rep = registry_scan.scan_registry()
    assert rep.available is True
    assert rep.force_installed == []
    assert rep.unreadable == []


def test_readable_force_install_is_flagged(monkeypatch):
    """A readable force root with PUP signatures still surfaces, no false gap."""
    ext = "aaaabbbbccccddddeeeeffffgggghhhh"
    _install_fake_winreg(
        monkeypatch,
        {
            r"HKCU\SOFTWARE\Google\Chrome\Extensions": [ext],
            rf"HKCU\SOFTWARE\Google\Chrome\Extensions\{ext}": {
                "update_url": "http://updates.example/x",
                "install_parameter": "clid=9911",
            },
        },
    )
    rep = registry_scan.scan_registry()
    assert rep.unreadable == []
    assert len(rep.force_installed) == 1
    rx = rep.force_installed[0]
    assert rx.ext_id == ext
    assert any("affiliate" in f for f in rx.flags)
    assert any("insecure http" in f for f in rx.flags)


def test_denied_policy_key_is_flagged_unreadable(monkeypatch):
    """A denied policy base (ExtensionInstallForcelist lives here) is a gap too."""
    _install_fake_winreg(
        monkeypatch,
        {r"HKLM\SOFTWARE\Policies\Google\Chrome\ExtensionInstallForcelist": "DENIED"},
    )
    rep = registry_scan.scan_registry()
    assert rep.forcelist == []
    assert any("ExtensionInstallForcelist" in loc for loc in rep.unreadable)


# ── command: refuse a clean verdict over unread locations ─────────────────────


def test_command_refuses_clean_when_unreadable(monkeypatch):
    """The phantom-clean regression: no findings + an unreadable location must exit
    non-zero and never print the green 'No registry-forced …' line."""
    rep = registry_scan.RegReport(
        available=True, unreadable=[r"HKLM\SOFTWARE\Google\Chrome\Extensions"]
    )
    monkeypatch.setattr(registry_scan, "scan_registry", lambda: rep)
    result = CliRunner().invoke(antivirus_app, ["registry"])
    assert result.exit_code == 1
    assert "No registry-forced Chrome extensions" not in result.output
    assert "NOT a clean result" in result.output


def test_command_reports_clean_only_when_genuinely_empty(monkeypatch):
    """Nothing found AND nothing unreadable is a real clean → exit 0 + the ✓ line."""
    monkeypatch.setattr(
        registry_scan, "scan_registry", lambda: registry_scan.RegReport(available=True)
    )
    result = CliRunner().invoke(antivirus_app, ["registry"])
    assert result.exit_code == 0
    assert "No registry-forced Chrome extensions" in result.output


def test_command_shows_findings_and_flags_incomplete_coverage(monkeypatch):
    """Findings present + an unreadable location: list the findings (exit 0) but warn
    the audit is incomplete rather than implying it was exhaustive."""
    rep = registry_scan.RegReport(
        available=True,
        force_installed=[registry_scan.RegExt(hive="HKCU", ext_id="x" * 32)],
        unreadable=[r"HKLM\SOFTWARE\Google\Chrome\Extensions"],
    )
    monkeypatch.setattr(registry_scan, "scan_registry", lambda: rep)
    result = CliRunner().invoke(antivirus_app, ["registry"])
    assert result.exit_code == 0
    assert "could not be read" in result.output
