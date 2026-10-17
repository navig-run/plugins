"""The `navig games grab` command itself.

Exists because a live run caught what the engine + route tests could not: the
command called a console helper that does not exist (`ch.status`), so every
invocation crashed with an AttributeError while 150 tests stayed green. The CLI
is a surface — it needs its own coverage.
"""

import pytest
from typer.testing import CliRunner

from navig_games.commands.games import games_app

runner_cli = CliRunner()


@pytest.fixture
def marked(monkeypatch):
    calls: list[tuple] = []

    def _mark(key, *, grabbed=True):
        calls.append((key, grabbed))
        if key == "itch:gone":
            return {"key": key, "changed": False, "error": "that game isn't free right now"}
        return {"key": key, "title": "NIGHTBELL", "store": "itch", "grabbed": grabbed,
                "changed": True, "status": "grabbed" if grabbed else None}

    monkeypatch.setattr("navig_games.engine.runner.mark_grabbed", _mark)
    return calls


def test_grab_marks_and_says_so(marked):
    res = runner_cli.invoke(games_app, ["grab", "itch:3697"])
    assert res.exit_code == 0, res.output
    assert marked == [("itch:3697", True)]
    assert "NIGHTBELL" in res.output


def test_grab_undo_passes_the_flag(marked):
    res = runner_cli.invoke(games_app, ["grab", "itch:3697", "--undo"])
    assert res.exit_code == 0, res.output
    assert marked == [("itch:3697", False)]


def test_grab_of_an_expired_game_exits_nonzero(marked):
    res = runner_cli.invoke(games_app, ["grab", "itch:gone"])
    assert res.exit_code == 1


def test_grab_json_is_machine_readable(marked):
    res = runner_cli.invoke(games_app, ["grab", "itch:3697", "--json"])
    assert res.exit_code == 0
    assert '"grabbed": true' in res.output.lower()
