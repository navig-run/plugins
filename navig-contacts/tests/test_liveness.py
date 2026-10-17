"""
Route liveness: recording that a handle no longer resolves, and acting on it.

The point of the feature is the distinction it draws. A verdict is about a
*route*, not a person, so a dead Telegram handle must not remove someone whose
phone still works -- and "never checked" must never be confused with "dead".
Both mistakes silently lose real contacts, which is what the tests here are
mostly about.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from navig_contacts.store import ContactStore
from navig_contacts.commands.contacts import app
from navig_contacts.engine import db as db_mod
from navig_contacts.engine.liveness import (
    DEAD_STATES, STATES, counts, read_verdicts, record, verdict_of,
)

runner = CliRunner()


def _run(*args):
    return runner.invoke(app, list(args))


def _meta(alias: str, network: str = "telegram"):
    with db_mod.get_db() as conn:
        row = conn.execute(
            "SELECT r.meta_json FROM contact_routes r JOIN contacts c ON c.id = r.contact_id "
            "WHERE c.alias = ? AND r.network = ?", (alias, network)).fetchone()
    return verdict_of(row[0]) if row else (None, None)


@pytest.fixture()
def people(book):
    """Three shapes that must be told apart."""
    store = ContactStore(db_path=book)
    # Telegram only, and it died: unreachable.
    store.add_contact(alias="ghost", display_name="Ghost",
                      routes=["telegram:ghost"])
    # Telegram died, but the phone works: still reachable.
    store.add_contact(alias="dual", display_name="Dual",
                      routes=["telegram:dual", "sms:+33612345678"])
    # Never checked. Not dead.
    store.add_contact(alias="quiet", display_name="Quiet",
                      routes=["telegram:quiet"])
    return book


# ---------------------------------------------------------------------------
# Recording a verdict
# ---------------------------------------------------------------------------

def test_a_verdict_lands_on_the_route(people):
    result = _run("flag", "--handle", "@ghost", "--state", "gone")
    assert result.exit_code == 0, result.output
    state, checked = _meta("ghost")
    assert state == "gone"
    assert checked


def test_the_at_sign_is_optional(people):
    assert _run("flag", "--handle", "ghost").exit_code == 0
    assert _meta("ghost")[0] == "gone", "gone is the default verdict"


def test_a_verdict_does_not_clear_the_rest_of_meta_json(people):
    """`meta_json` is shared; a liveness sweep owns one key in it, not the column."""
    with db_mod.get_db() as conn:
        conn.execute("UPDATE contact_routes SET meta_json = ? "
                     "WHERE address = 'ghost'", (json.dumps({"label": "work"}),))
    _run("flag", "--handle", "ghost", "--state", "gone")
    with db_mod.get_db() as conn:
        meta = json.loads(conn.execute(
            "SELECT meta_json FROM contact_routes WHERE address = 'ghost'").fetchone()[0])
    assert meta["label"] == "work"
    assert meta["resolve_state"] == "gone"


def test_a_handle_that_came_back_can_be_marked_ok(people):
    _run("flag", "--handle", "ghost", "--state", "gone")
    _run("flag", "--handle", "ghost", "--state", "ok")
    assert _meta("ghost")[0] == "ok"


def test_an_unknown_state_is_refused_with_the_list(people):
    result = _run("flag", "--handle", "ghost", "--state", "dead")
    assert result.exit_code == 2
    assert "not_a_user" in result.output
    assert _meta("ghost")[0] is None, "nothing was written"


def test_a_handle_the_book_does_not_have_is_named_not_just_counted(people):
    """A typo and a genuinely absent handle look identical in a total."""
    result = _run("flag", "--handle", "nobody")
    assert result.exit_code == 0
    assert "@nobody" in result.output


def test_dry_run_writes_nothing(people):
    result = _run("flag", "--handle", "ghost", "--dry-run")
    assert result.exit_code == 0
    assert _meta("ghost")[0] is None


def test_handle_and_from_file_are_mutually_exclusive(people, tmp_path):
    assert _run("flag", "--handle", "x", "--from-file", str(tmp_path / "f")).exit_code == 2
    assert _run("flag").exit_code == 2


# ---------------------------------------------------------------------------
# A sweep from a file
# ---------------------------------------------------------------------------

def test_a_jsonl_sweep_applies_every_line(people, tmp_path):
    path = tmp_path / "dead.jsonl"
    path.write_text(
        json.dumps({"handle": "ghost", "state": "gone"}) + "\n"
        + json.dumps({"handle": "@quiet", "state": "not_a_user"}) + "\n",
        encoding="utf-8")
    assert _run("flag", "--from-file", str(path)).exit_code == 0
    assert _meta("ghost")[0] == "gone"
    assert _meta("quiet")[0] == "not_a_user"


def test_one_bad_line_rejects_the_whole_file(tmp_path):
    """
    Importing half a sweep would leave the skipped handles looking unchecked
    when they had in fact been judged -- worse than importing none.
    """
    path = tmp_path / "bad.jsonl"
    path.write_text('{"handle": "a", "state": "gone"}\nnot json\n', encoding="utf-8")
    with pytest.raises(ValueError, match="bad.jsonl:2"):
        read_verdicts(path)


def test_a_line_with_an_unknown_state_names_the_line(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"handle": "a", "state": "banned"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="bad.jsonl:1"):
        read_verdicts(path)


def test_blank_lines_are_not_an_error(tmp_path):
    path = tmp_path / "ok.jsonl"
    path.write_text('\n{"handle": "a"}\n\n', encoding="utf-8")
    assert read_verdicts(path) == [("a", "gone")]


# ---------------------------------------------------------------------------
# What the export does with it -- the part that can lose real people
# ---------------------------------------------------------------------------

def test_export_drops_someone_whose_only_route_died(people):
    _run("flag", "--handle", "ghost", "--state", "gone")
    out = _run("export").output
    assert "Ghost" not in out
    assert "Quiet" in out


def test_export_KEEPS_someone_whose_phone_still_works(people):
    """
    The bug this feature could most easily introduce: dropping a reachable
    person because one of their transports died.
    """
    _run("flag", "--handle", "dual", "--state", "gone")
    assert "Dual" in _run("export").output


def test_export_keeps_a_handle_nobody_has_checked(people):
    """Unchecked is not dead. Treating it as dead would empty the export."""
    assert "Quiet" in _run("export").output


def test_include_dead_brings_them_back(people):
    _run("flag", "--handle", "ghost", "--state", "gone")
    assert "Ghost" in _run("export", "--include-dead").output


def test_a_contact_with_no_routes_at_all_is_not_called_unreachable(book):
    """Unrouted is a different problem, and not this one's to judge."""
    ContactStore(db_path=book).add_contact(alias="paper", display_name="Paper")
    assert "Paper" in _run("export").output


def test_the_export_says_how_many_it_left_out(people):
    _run("flag", "--handle", "ghost", "--state", "gone")
    result = _run("export")
    assert "1 unreachable" in result.output


def test_an_ok_verdict_does_not_drop_anyone(people):
    _run("flag", "--handle", "ghost", "--state", "ok")
    assert "Ghost" in _run("export").output


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def test_liveness_separates_unchecked_from_dead(people):
    _run("flag", "--handle", "ghost", "--state", "gone")
    out = _run("liveness").output
    assert "unchecked" in out
    assert "no live route left" in out

    with db_mod.get_db() as conn:
        tally = counts(conn)
    assert tally["gone"] == 1
    assert tally["unchecked"] == 3, "dual's two routes and quiet's one"


def test_show_prints_the_verdict_but_not_a_missing_one(people):
    _run("flag", "--handle", "ghost", "--state", "gone")
    assert "gone" in _run("show", "ghost").output
    assert "resolve_state" not in _run("show", "quiet").output


def test_every_dead_state_is_dead_and_ok_is_not(people):
    for state in STATES:
        with db_mod.get_db() as conn:
            record(conn, "ghost", state)
        dropped = "Ghost" not in _run("export").output
        assert dropped is (state in DEAD_STATES), f"{state} classified wrongly"


def test_a_re_import_does_not_wipe_a_verdict(book, tmp_path):
    """
    The correction has to outlive the import that provoked it.

    A verdict is a human decision made after a lookup; an import re-reads the
    same source and knows nothing about it. If the import overwrote the route,
    every sweep would be undone by the next export the operator imported, and
    silently -- an erased verdict is indistinguishable from an unchecked one.
    """
    import json as _json

    from navig_contacts.engine.telegram_bot import BOT_ID, import_bot_export

    export = tmp_path / "result.json"
    tg = "https://t.me/"
    entities = [{"type": "plain", "text": "Начинай общаться "},
                {"type": "text_link", "text": "Лера", "href": tg + "lera_m"}]
    export.write_text(_json.dumps({"messages": [
        {"type": "message", "from_id": BOT_ID, "text": "Лера, 22, Минск"},
        {"type": "message", "from_id": BOT_ID, "text": entities,
         "text_entities": entities},
    ]}, ensure_ascii=False), encoding="utf-8")

    with db_mod.get_db() as conn:
        import_bot_export(export, conn)
    assert _run("flag", "--handle", "lera_m", "--state", "gone").exit_code == 0

    with db_mod.get_db() as conn:
        import_bot_export(export, conn)

    assert _meta("lera_m")[0] == "gone", "the re-import erased the verdict"
