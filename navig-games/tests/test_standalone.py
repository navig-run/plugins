"""navig-games on its own: settings, the Epic cell and the agent tools say the right thing."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


@pytest.fixture
def no_navig(monkeypatch):
    """navig absent, as for `pip install navig-games` alone (blocked, not evicted)."""
    for mod in [m for m in sys.modules if m == "navig" or m.startswith("navig.")]:
        monkeypatch.setitem(sys.modules, mod, None)
    monkeypatch.setitem(sys.modules, "navig", None)
    from navig_sdk import host

    assert not host.navig_available()


def test_settings_round_trip_in_navigs_file_without_navig(no_navig, monkeypatch, tmp_path):
    pytest.importorskip("yaml")
    from navig_sdk import files

    from navig_games.engine import settings

    monkeypatch.setenv("NAVIG_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.yaml").write_text("plugins:\n  other:\n    keep: 1\n", encoding="utf-8")
    settings.set("country", "de")
    assert settings.country() == "DE"
    assert settings.get("missing", "dflt") == "dflt"
    data = files.safe_load_yaml(tmp_path / "config.yaml")
    assert data == {"plugins": {"other": {"keep": 1}, "games": {"country": "de"}}}


def test_an_unreadable_config_reads_as_unset_and_is_never_overwritten(no_navig, monkeypatch, tmp_path):
    pytest.importorskip("yaml")
    from navig_sdk import files

    from navig_games.engine import settings

    monkeypatch.setenv("NAVIG_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.yaml").write_text("plugins: [broken\n", encoding="utf-8")
    assert settings.get("country", "US") == "US"
    with pytest.raises(files.ConfigReadError):
        settings.set("country", "de")
    assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == "plugins: [broken\n"


def test_the_epic_cell_works_without_navig(no_navig, monkeypatch, tmp_path):
    """Epic sign-in runs on navig-browser + the shared vault now, so the cell reports the real
    state (no saved session here) and points at a login command that exists on its own."""
    from navig_games.commands import games
    from navig_games.engine.claim import epic

    # Pinned rather than read: an earlier test may already hold the vault singleton, and a
    # test must never depend on (or read) the operator's real saved sessions.
    monkeypatch.setattr(epic, "epic_session_present", lambda: (False, None))
    cell = games._epic_login_row(live=False)
    assert "needs navig" not in cell
    # CMD is fixed when the module is first imported, so accept either front door here.
    assert "not signed in" in cell and "login epic" in cell


def test_agent_tools_say_they_need_navig(no_navig):
    """Loaded under a throwaway name, so the real module in sys.modules is never touched."""
    import importlib.util

    import navig_games

    path = Path(navig_games.__file__).with_name("agent_tools.py")
    spec = importlib.util.spec_from_file_location("_agent_tools_probe", path)
    with pytest.raises(ImportError, match="needs navig"):
        spec.loader.exec_module(importlib.util.module_from_spec(spec))


def test_schedule_enable_refuses_rather_than_report_a_job_nothing_runs(no_navig, monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from navig_games.commands.games import games_app

    monkeypatch.setenv("NAVIG_CONFIG_DIR", str(tmp_path))
    res = CliRunner().invoke(games_app, ["schedule", "enable", "--job", "deals"])
    assert res.exit_code == 1, res.output
    assert "needs navig" in res.output and "deals notify" in res.output
    assert not (tmp_path / "scheduler" / "cron_jobs.json").exists()


def test_status_works_without_navig(no_navig, monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from navig_games.commands.games import games_app

    monkeypatch.setenv("NAVIG_CONFIG_DIR", str(tmp_path))
    res = CliRunner().invoke(games_app, ["schedule", "status"])
    assert res.exit_code == 0, res.output
    assert "off" in res.output


def test_deals_notify_never_claims_a_delivery_that_did_not_happen(no_navig, monkeypatch):
    from typer.testing import CliRunner

    from navig_games.commands.games import games_app
    from navig_games.engine import runner

    monkeypatch.setattr(runner, "run_deals_notify",
                        lambda **k: {"notified": 2, "titles": ["Squad", "Hades"], "checked": 9})
    res = CliRunner().invoke(games_app, ["deals", "notify"])
    assert res.exit_code == 0, res.output
    assert "Notified" not in res.output
    assert "Squad, Hades" in res.output and "needs navig" in res.output

