from __future__ import annotations

import json

from navig_media.cli import SUITE, main, scan


def test_suite_covers_the_seven_components():
    """The bundle's whole contract: one entry per suite package, with both commands."""
    names = [row[0] for row in SUITE]
    assert names == [
        "navig-generate",
        "navig-audio",
        "navig-text",
        "navig-video",
        "navig-pipeline",
        "navig-download",
        "navig-social",
    ]
    for package, does, standalone, verb in SUITE:
        assert package == standalone  # dist and console script share the name
        assert verb.startswith("navig ")


def test_scan_reports_presence_not_raises(monkeypatch):
    """Missing packages are reported, not raised — a partial studio still prints."""
    from navig_media import cli

    def fake_version(name: str) -> str:
        if name == "navig-audio":
            return "0.3.0"
        raise cli.PackageNotFoundError(name)

    monkeypatch.setattr(cli, "version", fake_version)
    rows = scan()
    assert [c.package for c in rows] == [row[0] for row in SUITE]  # table order is stable
    by_name = {c.package: c for c in rows}
    assert by_name["navig-audio"].installed == "0.3.0"
    assert by_name["navig-generate"].installed is None


def test_json_output_and_exit_codes(monkeypatch, capsys):
    """--json is machine-parseable; exit 1 signals an incomplete suite."""
    from navig_media import cli

    monkeypatch.setattr(cli, "version", lambda name: "1.2.3")
    assert main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["suite"]) == 7
    assert all(item["installed"] == "1.2.3" for item in payload["suite"])

    def none(name: str) -> str:
        raise cli.PackageNotFoundError(name)

    monkeypatch.setattr(cli, "version", none)
    assert main(["--json"]) == 1


def test_unknown_argument_fails_loud(capsys):
    assert main(["--typo"]) == 2
    assert "unknown argument" in capsys.readouterr().err


def test_help_prints_usage_for_the_standalone_guard(capsys):
    """core/tests/quality/test_standalone_plugins_run_without_navig.py runs every
    standalone CLI --help and requires 'usage' or '--help' in the output."""
    assert main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "usage" in out.lower() or "--help" in out
