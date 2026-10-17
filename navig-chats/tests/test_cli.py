"""``navig chats`` end to end through the Typer app: import → list → search → show → export → restore."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from navig_chats.commands.chats import app

runner = CliRunner()


def run(*args: str):
    return runner.invoke(app, list(args), env={"COLUMNS": "120"})


def test_cli_round_trip(db_path, sample_export, tmp_path):
    res = run("import", str(sample_export), "--json")
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["added"] == 3

    res = run("list", "--sort", "oldest", "--json")
    assert [r["native_id"] for r in json.loads(res.output)["items"]] == ["c-rust", "c-db", "c-trip"]

    res = run("search", "lifetimes")
    assert res.exit_code == 0 and "Rust lifetimes" in res.output

    res = run("search", "-python")
    assert res.exit_code == 2  # nothing to search for is a usage error

    res = run("show", "c-db")
    assert res.exit_code == 0 and "SQLite with FTS5" in res.output

    res = run("show", "nope")
    assert res.exit_code == 2

    res = run("folders", "add", "Work")
    assert res.exit_code == 0
    fid = json.loads(run("folders", "list", "--json").output)[0]["id"]
    assert run("move", "c-db", "--to", fid).exit_code == 0
    assert json.loads(run("list", "--folder", fid, "--json").output)["total"] == 1

    out = tmp_path / "all.zip"
    res = run("export", "--all", "-o", str(out))
    assert res.exit_code == 0 and out.exists()
    one = tmp_path / "one.md"
    assert run("export", "c-trip", "-o", str(one)).exit_code == 0
    assert one.read_text(encoding="utf-8").startswith("---\n")
    assert run("export", "c-trip", "c-db", "-o", str(tmp_path / "two.md")).exit_code == 2

    stats = json.loads(run("stats", "--json").output)
    assert stats["conversations"] == 3 and stats["index_intact"] is True
    assert run("reindex").exit_code == 0
    assert run("restore", str(out)).exit_code == 0


def test_import_of_a_missing_or_foreign_file_is_a_usage_error(db_path, tmp_path):
    assert run("import", str(tmp_path / "nope.zip")).exit_code == 2
    bad = tmp_path / "x.json"
    bad.write_text('{"not": "a list"}', encoding="utf-8")
    assert run("import", str(bad)).exit_code == 2
