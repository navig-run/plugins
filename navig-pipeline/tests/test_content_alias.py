"""`navig content` is canonical; `navig pipeline` stays a working, deprecated alias.

Same app under both names: verbs, flags, exit codes and --json stdout are identical; the
old name adds one deprecation line on STDERR only, so scripts parsing stdout keep working.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import typer
from typer.testing import CliRunner

from navig_pipeline import cli
from navig_pipeline.commands.pipeline import pipeline_app

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _runner() -> CliRunner:
    try:
        return CliRunner(mix_stderr=False)  # Click < 8.2
    except TypeError:
        return CliRunner()  # Click >= 8.2 keeps stderr separate by default


def _root() -> typer.Typer:
    root = typer.Typer()
    root.add_typer(pipeline_app, name="content")
    root.add_typer(pipeline_app, name="pipeline")
    return root


def test_both_names_give_identical_json_on_stdout() -> None:
    runner = _runner()
    new = runner.invoke(_root(), ["content", "status", "--json"])
    old = runner.invoke(_root(), ["pipeline", "status", "--json"])
    assert new.exit_code == old.exit_code == 0, (new.output, old.output)
    assert json.loads(new.stdout) == json.loads(old.stdout)


def test_only_the_old_name_warns_and_only_on_stderr() -> None:
    runner = _runner()
    new = runner.invoke(_root(), ["content", "status", "--json"])
    old = runner.invoke(_root(), ["pipeline", "status", "--json"])
    assert "deprecated" not in new.stderr and "deprecated" not in new.stdout
    assert "navig content" in old.stderr and "deprecated" in old.stderr
    assert "deprecated" not in old.stdout


def test_both_names_and_both_scripts_are_declared() -> None:
    meta = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    cmds = meta["entry-points"]["navig.commands"]
    assert cmds["content"] == cmds["pipeline"] == "navig_pipeline.commands.pipeline:pipeline_app"
    assert meta["scripts"]["navig-content"] == "navig_pipeline.cli:main_content"
    assert meta["scripts"]["navig-pipeline"] == "navig_pipeline.cli:main"
    assert callable(cli.main_content)
