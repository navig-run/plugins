"""``navig notes`` end to end through the Typer app: add → list → search → show → export → rm → status."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from conftest import dock_note
from navig_notes.commands.notes import app

runner = CliRunner()


def run(*args: str):
    return runner.invoke(app, list(args), env={"COLUMNS": "120"})


def test_cli_round_trip(db_path, store, tmp_path):
    store.sync([dock_note("dock-1", body="Issue twelve: check the retry path", tags=["work"])], [])

    res = run("add", "Buy", "milk", "--json")
    assert res.exit_code == 0, res.output
    made = json.loads(res.output)
    assert made["scope"] == "global" and made["body"] == "Buy milk" and made["origin"] == "cli"

    res = run("add", "about this page", "--url", "https://example.com/a?utm_source=x", "--json")
    page = json.loads(res.output)
    assert page["scope"] == "page" and page["pageKey"] == "example.com/a"

    res = run("add", "x", "--scope", "site")
    assert res.exit_code == 2, "a site note needs a page address"

    res = run("list")
    assert res.exit_code == 0 and "Issue twelve" in res.output and "every site" in res.output
    assert len(json.loads(run("list", "--json").output)) == 3
    assert [n["id"] for n in json.loads(run("list", "--site", "github.com", "--json").output)] == ["dock-1"]
    assert run("list", "--scope", "nope").exit_code == 2

    res = run("search", "retry")
    assert res.exit_code == 0 and "Issue twelve" in res.output
    assert "Nothing matches" in run("search", "zebra").output

    res = run("show", "dock")
    assert res.exit_code == 0 and "check the retry path" in res.output and "#work" in res.output
    assert run("show", "nope").exit_code == 2

    out = tmp_path / "notes.json"
    res = run("export", str(out))
    assert res.exit_code == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["format"] == "navig-notes" and len(doc["notes"]) == 3
    md = tmp_path / "notes.md"
    run("export", str(md))
    assert "# NAVIG notes" in md.read_text(encoding="utf-8")

    res = run("rm", made["id"][:8])
    assert res.exit_code == 0
    assert len(json.loads(run("list", "--json").output)) == 2

    res = run("status")
    assert res.exit_code == 0 and "Last Dock sync" in res.output
    st = json.loads(run("status", "--json").output)
    assert st["notes"] == 2 and st["deleted"] == 1
