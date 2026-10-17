"""
`navig contacts resolve` — obtaining verdicts for this book, and feeding `flag`.

Two things are being tested and they are not the same:

* the book side — which handles are picked up, what is written where, and that
  an *unchecked* handle never becomes a verdict;
* the hand-off — that what `resolve` writes is exactly what `flag --from-file`
  consumes, so the sweep is one command rather than a format to get right by
  hand.

Telegram is never contacted: the resolver is replaced wholesale, so these tests
stay offline and fast.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from navig.store.contacts import ContactStore
from navig_contacts.commands.contacts import app
from navig_contacts.engine import db as db_mod

runner = CliRunner()

#: What the fake resolver answers, keyed by handle.
_ANSWERS: dict[str, dict] = {}


def _install(monkeypatch, answers):
    """Replace the resolver with one that consults `answers` and never dials."""
    import navig.telegram.resolve as resolve_mod

    calls: list[list[str]] = []

    async def fake(handles, **kw):
        calls.append(list(handles))
        verdicts, unchecked = [], []
        for h in handles:
            entry = _ANSWERS.get(h.lower(), {})
            if "state" in entry:
                verdicts.append({"handle": h, "state": entry["state"]})
            else:
                unchecked.append(h)
        return {"verdicts": verdicts, "unchecked": unchecked,
                "resolved": len(verdicts), "checked": len(list(handles)),
                "aborted": None}

    _ANSWERS.clear()
    _ANSWERS.update({k.lower(): v for k, v in answers.items()})
    monkeypatch.setattr(resolve_mod, "resolve_handles", fake)
    return calls


def _run(*args):
    return runner.invoke(app, list(args))


def _unchecked():
    with db_mod.get_db() as conn:
        return sorted(r[0] for r in conn.execute(
            "SELECT address FROM contact_routes "
            "WHERE network = 'telegram' COLLATE NOCASE "
            "AND json_extract(meta_json, '$.resolve_state') IS NULL"))


def _state(alias: str):
    with db_mod.get_db() as conn:
        row = conn.execute(
            "SELECT r.meta_json FROM contact_routes r "
            "JOIN contacts c ON c.id = r.contact_id WHERE c.alias = ?",
            (alias,)).fetchone()
    if row is None or not row[0]:
        return None
    return json.loads(row[0]).get("resolve_state")


@pytest.fixture()
def three(book):
    """Two handles the book has never judged, one it already has."""
    store = ContactStore(db_path=book)
    store.add_contact(alias="ghost", display_name="Ghost", routes=["telegram:ghost"])
    store.add_contact(alias="quiet", display_name="Quiet", routes=["telegram:quiet"])
    store.add_contact(alias="known", display_name="Known", routes=["telegram:known"])
    with db_mod.get_db() as conn:
        conn.execute(
            "UPDATE contact_routes SET meta_json = ? WHERE address = 'known'",
            (json.dumps({"resolve_state": "gone"}),))
    return book


# ── which handles get checked ────────────────────────────────────────────────


def test_only_unchecked_routes_are_picked_up(three, monkeypatch):
    calls = _install(monkeypatch, {})
    assert _run("resolve", "--dry-run").exit_code == 0
    assert calls == [], "a dry run must not contact Telegram"
    assert _unchecked() == ["ghost", "quiet"]


def test_the_handle_already_judged_is_not_asked_about_again(three, monkeypatch):
    calls = _install(monkeypatch, {"ghost": {"state": "gone"},
                                   "quiet": {"state": "ok"}})
    assert _run("resolve", "--write").exit_code == 0
    asked = [h for batch in calls for h in batch]
    assert "known" not in asked, "an existing verdict is not re-spent"


# ── what gets recorded ───────────────────────────────────────────────────────


def test_write_records_the_verdicts(three, monkeypatch):
    _install(monkeypatch, {"ghost": {"state": "gone"}, "quiet": {"state": "ok"}})
    assert _run("resolve", "--write").exit_code == 0
    assert _state("ghost") == "gone"
    assert _state("quiet") == "ok"
    assert _state("known") == "gone", "the existing verdict was left alone"
    assert _unchecked() == [], "every handle in the book now has an answer"


def test_an_unchecked_handle_is_never_recorded_as_dead(three, monkeypatch):
    """
    The failure this whole command must not introduce: no answer is not a
    verdict, and `--write` is the only path that touches the book.
    """
    _install(monkeypatch, {"ghost": {"state": "gone"}, "quiet": {}})
    assert _run("resolve", "--write").exit_code == 0
    assert _state("ghost") == "gone"
    assert _state("quiet") is None, "quiet was never judged, so it stays unjudged"
    assert "quiet" in _unchecked()


def test_a_dry_run_writes_nothing(three, monkeypatch):
    _install(monkeypatch, {"ghost": {"state": "gone"}})
    assert _run("resolve", "--dry-run").exit_code == 0
    assert _state("ghost") is None
    assert _unchecked() == ["ghost", "quiet"]


def test_nothing_to_check_is_not_an_error(three, monkeypatch):
    _install(monkeypatch, {"ghost": {"state": "ok"}, "quiet": {"state": "ok"}})
    assert _run("resolve", "--write").exit_code == 0
    result = _run("resolve", "--write")
    assert result.exit_code == 0
    assert "Nothing to check" in result.output


# ── the hand-off to `flag --from-file` ───────────────────────────────────────


def test_the_output_is_exactly_what_flag_reads(three, monkeypatch, tmp_path):
    """`resolve` -> file -> `flag` has to work with no editing in between."""
    _install(monkeypatch, {"ghost": {"state": "gone"}, "quiet": {}})
    out = tmp_path / "dead.jsonl"

    assert _run("resolve", "--out", str(out)).exit_code == 0
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines, "verdicts must reach the file"
    for line in lines:
        rec = json.loads(line)
        assert set(rec) == {"handle", "state"}, f"flag --from-file reads {rec}"

    assert _run("flag", "--from-file", str(out)).exit_code == 0
    assert _state("ghost") == "gone"
    assert _state("quiet") is None, "flag must not record what resolve did not judge"


def test_jsonl_goes_to_stdout_when_no_file_is_asked_for(three, monkeypatch):
    _install(monkeypatch, {"ghost": {"state": "gone"}})
    result = _run("resolve")
    assert result.exit_code == 0
    first = next(ln for ln in result.output.splitlines()
                  if ln.startswith("{"))
    assert json.loads(first) == {"handle": "ghost", "state": "gone"}


def test_a_file_of_handles_overrides_the_book(three, monkeypatch, tmp_path):
    source = tmp_path / "handles.txt"
    source.write_text("someone_else\n@another\n", encoding="utf-8")
    calls = _install(monkeypatch, {"someone_else": {"state": "gone"},
                                   "another": {"state": "ok"}})

    assert _run("resolve", "--from-file", str(source), "--write").exit_code == 0
    asked = [h for batch in calls for h in batch]
    assert sorted(asked) == ["another", "someone_else"]
    assert _state("ghost") is None, "the book was not the source this time"


def test_an_older_navig_is_told_to_upgrade_not_reinstall(monkeypatch, three, tmp_path):
    """
    Two failures wear the same `ImportError`, and the obvious advice is wrong
    for one of them.

    A navig older than 3.27.0 HAS the telegram extra — telling someone to
    install `navig[telegram]` sends them to reinstall what they already have,
    while the real answer is that the package is too old to contain
    `navig.telegram.resolve`.
    """
    import importlib.abc
    import sys

    class _BlockResolve(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name == "navig.telegram.resolve":
                raise ModuleNotFoundError(
                    "No module named 'navig.telegram.resolve'")
            return None

    monkeypatch.setattr(sys, "meta_path", [_BlockResolve()] + sys.meta_path)
    monkeypatch.setitem(sys.modules, "navig.telegram.resolve", None)
    sys.modules.pop("navig.telegram.resolve", None)

    result = _run("resolve", "--limit", "1")
    assert result.exit_code == 1
    assert "Upgrade navig" in result.output, result.output
    assert "navig[telegram]" not in result.output, (
        "navig is installed — telling them to add its telegram extra is the "
        "wrong fix")
