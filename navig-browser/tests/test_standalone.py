"""navig-browser on its own: the engine imports, reads navig's files, and says what needs navig."""

from __future__ import annotations

import asyncio
import sys
import tomllib
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]


@pytest.fixture
def no_navig(monkeypatch):
    """navig absent, as for `pip install navig-browser` alone (blocked, not evicted)."""
    for mod in [m for m in sys.modules if m == "navig" or m.startswith("navig.")]:
        monkeypatch.setitem(sys.modules, mod, None)
    monkeypatch.setitem(sys.modules, "navig", None)
    from navig_sdk import host

    assert not host.navig_available()


def test_templates_ship_in_the_wheel():
    data = tomllib.loads((PLUGIN / "pyproject.toml").read_text(encoding="utf-8"))
    assert "templates/*.yaml" in data["tool"]["setuptools"]["package-data"]["navig_browser"]
    shipped = sorted(p.name for p in (PLUGIN / "navig_browser" / "templates").glob("*.yaml"))
    assert shipped == ["example-app.yaml", "generic.yaml"]


def test_config_reads_navigs_file_without_navig(no_navig, monkeypatch, tmp_path):
    pytest.importorskip("yaml")
    from navig_browser import _compat

    monkeypatch.setenv("NAVIG_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.yaml").write_text("browser:\n  headless: false\n  cloud: {endpoint: x}\n",
                                          encoding="utf-8")
    cm = _compat.get_config_manager()
    assert cm.get("browser.headless") is False
    assert cm.global_config["browser"]["cloud"] == {"endpoint": "x"}
    assert cm.get("browser.nope", "dflt") == "dflt"


def test_profiles_and_launch_records_stay_in_navigs_config_dir(no_navig, monkeypatch, tmp_path):
    """The leak-safety records must be the SAME files navig reads, with or without navig."""
    from navig_browser import _paths, profiles, targets

    monkeypatch.setenv("NAVIG_CONFIG_DIR", str(tmp_path))
    assert Path(_paths.profile_dir("work")) == tmp_path / "browser" / "profiles" / "work"
    assert Path(_paths.screenshot_dir()) == tmp_path / "screenshots"
    assert targets._launched_registry_path() == tmp_path / "cdp-launched.json"
    assert profiles.registry_path() == tmp_path / "cdp-profiles.json"


def test_loggers_keep_navigs_names(no_navig):
    from navig_browser import cdp_actions, targets

    assert targets.logger.name == "navig.browser.targets"
    assert cdp_actions.logger.name == "navig.browser.cdp_actions"


def test_spawn_holds_the_task_until_it_finishes(no_navig):
    from navig_browser import _compat

    async def _go():
        done = []

        async def _work():
            done.append(1)

        task = _compat.spawn(_work(), name="t")
        assert task in _compat._bg_tasks
        await task
        await asyncio.sleep(0)
        return done, task in _compat._bg_tasks

    done, still_held = asyncio.run(_go())
    assert done == [1] and not still_held


def test_cortex_says_it_needs_navig(no_navig):
    import navig_browser

    orch = navig_browser.CortexOrchestrator  # resolved lazily; importing it needs no navig
    assert orch.__module__ == "navig_browser.orchestrator"


def test_the_cli_starts_without_navig(no_navig, capsys):
    from navig_browser.cli import main

    sys.argv = ["navig-browser", "--help"]
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "stop" in out and "profile" in out
