"""Exit-code honesty for the two skeleton verbs (the whole-tree gate's contract).

A command that prints a failure must not exit 0: `status` is informational (exit 0
always — it REPORTS absence), `doctor` gates (exit 1 when a required check fails),
usage errors exit 2.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from navig_video.cli import _standalone_app

runner = CliRunner()


def test_unknown_verb_exits_2(tmp_path: Path):
    result = runner.invoke(
        _standalone_app(), ["render", "--workspace", str(tmp_path)]
    )
    assert result.exit_code == 2


def test_status_never_gates(tmp_path: Path):
    # status's whole job is reporting what is missing — it must not fail on it
    result = runner.invoke(
        _standalone_app(), ["status", "--workspace", str(tmp_path / "nope")]
    )
    assert result.exit_code == 0


def test_doctor_gates_on_missing_workspace(tmp_path: Path):
    result = runner.invoke(
        _standalone_app(), ["doctor", "--workspace", str(tmp_path / "nope")]
    )
    assert result.exit_code == 1


def test_no_args_is_help_not_silent_success():
    result = runner.invoke(_standalone_app(), [])
    assert result.exit_code != 0  # no_args_is_help → usage exit
