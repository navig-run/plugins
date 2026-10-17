"""`navig video status|doctor` — the skeleton's two verbs.

status is informational (exit 0 whatever the state); doctor is a gate (exit 1 when a
REQUIRED piece is missing). Both must work with no workspace present at all — a
plugin that cannot even report a missing workspace is useless for diagnosis.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from navig_video.cli import _standalone_app
from navig_video.commands.video import video_app

runner = CliRunner()


@pytest.fixture()
def app():
    return _standalone_app()


def test_help_lists_status_and_doctor(app):
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "status" in result.output
    assert "doctor" in result.output


def test_status_exits_0_even_without_workspace(app, tmp_path: Path):
    result = runner.invoke(app, ["status", "--workspace", str(tmp_path / "absent")])
    assert result.exit_code == 0, result.output
    assert "absent" in result.output
    assert "planned" in result.output  # render verbs honestly reported as planned


def test_status_json_shape(app, tmp_path: Path):
    result = runner.invoke(
        app, ["status", "--workspace", str(tmp_path), "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["workspaceExists"] is True
    assert "ffmpeg" in payload
    assert isinstance(payload["verbs"], list)
    # `wired` is computed from the command tree: the kinetic group is mounted,
    # the not-yet-ported verbs must not claim to exist
    wired = {v["verb"]: v["wired"] for v in payload["verbs"]}
    assert wired["kinetic render"] is True and wired["kinetic plan"] is True
    assert wired["bulletin"] is False and wired["variant"] is False


def test_doctor_missing_workspace_exits_1(app, tmp_path: Path):
    result = runner.invoke(app, ["doctor", "--workspace", str(tmp_path / "absent")])
    assert result.exit_code == 1, result.output
    assert "workspace" in result.output


def test_doctor_json_reports_checks(app, tmp_path: Path):
    result = runner.invoke(
        app, ["doctor", "--workspace", str(tmp_path), "--json"]
    )
    # workspace exists here → the required checks that remain (ffmpeg) decide
    payload = json.loads(result.output)
    assert payload["ok"] in (True, False)
    assert any(c["check"] == "workspace" and c["ok"] for c in payload["checks"])
    assert any(c["check"] == "ffmpeg" for c in payload["checks"])
    assert result.exit_code == (0 if payload["ok"] else 1)


def test_doctor_on_real_workspace_layout(app, tmp_path: Path):
    (tmp_path / "out").mkdir()
    (tmp_path / "episodes.py").write_text("EPISODES = {}\n", encoding="utf-8")
    (tmp_path / "plates").mkdir()
    (tmp_path / "vo").mkdir()
    result = runner.invoke(app, ["doctor", "--workspace", str(tmp_path), "--json"])
    payload = json.loads(result.output)
    by_name = {c["check"]: c for c in payload["checks"]}
    assert by_name["workspace"]["ok"]
    assert by_name["master texts"]["ok"]
    assert by_name["episodes.py"]["ok"]
    assert by_name["plates"]["ok"]
    # required checks other than workspace layout are ffmpeg/ffprobe presence
    if result.exit_code == 0:
        assert by_name["ffmpeg"]["ok"]


def test_mounted_on_video_app_for_navig():
    """`navig video …` resolves through the group navig mounts."""
    import click
    from typer.main import get_command

    root = get_command(video_app)
    assert isinstance(root, click.Group)
    ctx = click.Context(root)
    assert root.get_command(ctx, "status") is not None
    assert root.get_command(ctx, "doctor") is not None


def test_json_flag_does_not_leak_secrets(app, tmp_path: Path):
    """The JSON/status output must be safe to paste — no env dumps, no key names."""
    result = runner.invoke(app, ["status", "--workspace", str(tmp_path), "--json"])
    assert "API_KEY" not in result.output
    assert "TOKEN" not in result.output
