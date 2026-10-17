"""
The bot chat-export importer.

Telegram Desktop writes two different files and they are not interchangeable:
`contacts.json` is the address book, and a *chat* export's `result.json` is a
conversation. When that conversation is with a matchmaking bot, every profile
card in it is a person no other importer can see — the address-book importer
reads `contacts.json` only, so without this the export is unreadable.
"""

from __future__ import annotations

import json

from navig_contacts.commands.contacts import _detect_format
from navig_contacts.engine import db as db_mod
from navig_contacts.engine.telegram_bot import (
    BOT_ID, import_bot_export, parse_telegram_export,
)

TG = "https://t.me/"


def _bot(text, entities=None, photo=None, **extra):
    msg = {"type": "message", "from_id": BOT_ID, "text": text}
    if entities is not None:
        msg["text_entities"] = entities
    if photo is not None:
        msg["photo"] = photo
    msg.update(extra)
    return msg


def _export(tmp_path, messages, name="result.json"):
    path = tmp_path / name
    path.write_text(json.dumps({"name": "Leo", "messages": messages},
                               ensure_ascii=False), encoding="utf-8")
    return path


CARD = _bot("Лера, 22, Минск", photo="photos/photo_1@01-01-2024.jpg")
MATCH = _bot(
    [{"type": "plain", "text": "Начинай общаться "},
     {"type": "text_link", "text": "Лера", "href": TG + "lera_m"}],
    entities=[{"type": "plain", "text": "Начинай общаться "},
              {"type": "text_link", "text": "Лера", "href": TG + "lera_m"}],
)


# --------------------------------------------------------------------------
# Telling the two exports apart
# --------------------------------------------------------------------------

def test_a_chat_export_is_not_mistaken_for_a_contacts_list(tmp_path):
    assert _detect_format(_export(tmp_path, [CARD, MATCH])) == "telegram-bot"


def test_a_contacts_list_still_goes_to_the_address_book_importer(tmp_path):
    path = tmp_path / "contacts.json"
    path.write_text(json.dumps(
        {"about": "…", "contacts": {"list": [{"phone_number": "+33612345678"}]}}),
        encoding="utf-8")
    assert _detect_format(path) == "telegram"


def test_an_html_export_folder_is_recognised(tmp_path):
    (tmp_path / "messages.html").write_text("<html></html>", encoding="utf-8")
    assert _detect_format(tmp_path) == "telegram-bot"


def test_a_folder_of_vcards_still_wins(tmp_path):
    (tmp_path / "a.vcf").write_text("BEGIN:VCARD\nEND:VCARD\n", encoding="utf-8")
    (tmp_path / "messages.html").write_text("<html></html>", encoding="utf-8")
    assert _detect_format(tmp_path) == "vcf"


# --------------------------------------------------------------------------
# Reading the cards
# --------------------------------------------------------------------------

def test_a_match_carries_the_card_that_came_before_it(tmp_path):
    [profile] = parse_telegram_export(_export(tmp_path, [CARD, MATCH]))
    assert profile["username"] == "lera_m"
    assert profile["full_name"] == "Лера"
    assert profile["age"] == "22"
    assert profile["city"] == "Минск"
    assert profile["photo_rel_path"] == "photos/photo_1@01-01-2024.jpg"


def test_a_reaction_between_the_card_and_the_match_does_not_break_the_scan(tmp_path):
    """The scan skips the operator's own messages rather than stopping at one."""
    mine = {"type": "message", "from_id": "user99", "text": "👍"}
    [profile] = parse_telegram_export(_export(tmp_path, [CARD, mine, MATCH]))
    assert profile["full_name"] == "Лера"


def test_a_card_under_a_header_line_is_still_found(tmp_path):
    """
    The bot sometimes prefixes the card with a header. A header ends in ':',
    which is what keeps a free-text line that merely looks like a card
    ("...", 24, ...) from being read as one.
    """
    carded = _bot("Кому-то понравилась твоя анкета:\n\nЛера), 22, Минск – Напиши мне ))")
    [profile] = parse_telegram_export(_export(tmp_path, [carded, MATCH]))
    assert (profile["full_name"], profile["city"]) == ("Лера)", "Минск")


def test_a_message_with_no_match_marker_is_not_a_contact(tmp_path):
    chatter = _bot(
        [{"type": "text_link", "text": "Лера", "href": TG + "lera_m"}],
        entities=[{"type": "text_link", "text": "Лера", "href": TG + "lera_m"}])
    assert parse_telegram_export(_export(tmp_path, [CARD, chatter])) == []


def test_the_bot_s_own_links_are_never_imported_as_people(tmp_path):
    """`t.me/leomatchbot` is the bot advertising itself, not a match."""
    selfish = _bot(
        [{"type": "plain", "text": "симпатия "},
         {"type": "text_link", "text": "Leo", "href": TG + "leomatchbot"}],
        entities=[{"type": "plain", "text": "симпатия "},
                  {"type": "text_link", "text": "Leo", "href": TG + "leomatchbot"}])
    assert parse_telegram_export(_export(tmp_path, [CARD, selfish])) == []


# --------------------------------------------------------------------------
# Writing them into the shared book
# --------------------------------------------------------------------------

def test_an_imported_profile_becomes_a_contact_with_a_reachable_route(book):
    """
    The capability the tool this came from did not have: the username lands as
    an identifier *and* a route, so `navig dispatch send` can reach them.
    """
    path = _export(book.parent, [CARD, MATCH])
    with db_mod.get_db() as conn:
        summary = import_bot_export(path, conn)
        assert (summary.total, summary.inserted) == (1, 1)

        row = conn.execute(
            "SELECT alias, display_name, city, tier FROM contacts").fetchone()
        assert tuple(row) == ("lera_m", "Лера", "Минск", "handle")
        assert [tuple(r) for r in conn.execute(
            "SELECT kind, value_norm FROM contact_identifiers")] == [
                ("telegram", "lera_m")]
        assert [tuple(r) for r in conn.execute(
            "SELECT network, address FROM contact_routes")] == [
                ("telegram", "lera_m")]


def test_importing_the_same_export_twice_adds_nobody(book):
    """The archive gets re-exported; a second run must recognise everyone."""
    path = _export(book.parent, [CARD, MATCH])
    with db_mod.get_db() as conn:
        import_bot_export(path, conn)
        second = import_bot_export(path, conn)
        assert (second.inserted, second.duplicates) == (0, 1)
        assert conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 1


def test_a_profile_attaches_to_whoever_already_owns_the_handle(book):
    """
    Someone imported from a vCard as `Valeria` is the same person the bot calls
    `lera_m`. Matching on the identifier, not the alias, is what stops the
    address book growing a second copy of her.
    """
    from navig_contacts.store import ContactStore

    ContactStore(db_path=book).add_contact(
        alias="valeria", display_name="Valeria", routes=["telegram:lera_m"])

    path = _export(book.parent, [CARD, MATCH])
    with db_mod.get_db() as conn:
        summary = import_bot_export(path, conn)
        assert (summary.inserted, summary.duplicates) == (0, 1)
        assert conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 1
        assert conn.execute(
            "SELECT alias, city FROM contacts").fetchone()["city"] == "Минск"


def test_the_photo_is_copied_out_with_a_portable_path(book, tmp_path):
    """
    A stored `photos\\lera.jpg` does not resolve off Windows, so the path
    written onto the contact is always forward-slashed.
    """
    export = tmp_path / "export"
    (export / "photos").mkdir(parents=True)
    (export / "photos" / "photo_1@01-01-2024.jpg").write_bytes(b"\xff\xd8jpeg")
    path = _export(export, [CARD, MATCH])

    with db_mod.get_db() as conn:
        import_bot_export(path, conn, photos_dir=tmp_path / "photos")
        stored = conn.execute(
            "SELECT photo_path FROM contacts").fetchone()[0]

    assert stored == "photos/lera_m.jpg"
    assert (tmp_path / "photos" / "lera_m.jpg").read_bytes() == b"\xff\xd8jpeg"


def test_one_unreadable_card_does_not_abort_the_export(book, monkeypatch):
    """An export is thousands of messages; one bad card must not lose the rest."""
    import navig_contacts.engine.telegram_bot as mod

    second = _bot(
        [{"type": "plain", "text": "симпатия "},
         {"type": "text_link", "text": "Аня", "href": TG + "anya_k"}],
        entities=[{"type": "plain", "text": "симпатия "},
                  {"type": "text_link", "text": "Аня", "href": TG + "anya_k"}])
    path = _export(book.parent, [CARD, MATCH, _bot("Аня, 25, Минск"), second])

    real = mod._write_profile
    calls = {"n": 0}

    def explode(conn, profile, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("bad card")
        return real(conn, profile, *args, **kwargs)

    monkeypatch.setattr(mod, "_write_profile", explode)
    with db_mod.get_db() as conn:
        summary = import_bot_export(path, conn)
        assert (summary.total, summary.errors, summary.inserted) == (2, 1, 1)
        assert conn.execute(
            "SELECT alias FROM contacts").fetchone()[0] == "anya_k"


# ---------------------------------------------------------------------------
# An import that leaves no audit row
# ---------------------------------------------------------------------------

def _runs():
    with db_mod.get_db() as conn:
        return conn.execute(
            "SELECT source, kind, total, inserted, duplicates, errors, "
            "finished_at FROM contact_import_runs ORDER BY id").fetchall()


def test_a_bot_import_records_a_run(book, tmp_path):
    """
    "What did this import add" is only answerable if the run was written down.

    The vCard path has always recorded one; the Telegram paths did not, so a
    real import of 891 profiles left nothing but a backup. A backup says
    something happened, not what.
    """
    from typer.testing import CliRunner

    from navig_contacts.commands.contacts import app

    (tmp_path / "book").mkdir()
    path = _export(tmp_path / "book", [CARD, MATCH])

    result = CliRunner().invoke(app, ["import", str(path)])
    assert result.exit_code == 0, result.output

    [run] = _runs()
    assert (run[0], run[1]) == ("import", "json")
    assert run[2] == 1, "the export held one profile"
    assert run[3] == 1
    assert run[4] == 0
    assert run[5] == 0
    assert run[6], "an unclosed row reads as a run that never finished"
    assert "run #" in result.output, "the operator is told which run it was"


def test_a_second_bot_import_records_its_own_row(book, tmp_path):
    """Idempotence still leaves a trail: 0 added is a result worth recording."""
    from typer.testing import CliRunner

    from navig_contacts.commands.contacts import app

    (tmp_path / "book").mkdir()
    path = _export(tmp_path / "book", [CARD, MATCH])

    CliRunner().invoke(app, ["import", str(path)])
    second = CliRunner().invoke(app, ["import", str(path)])
    assert second.exit_code == 0, second.output

    runs = _runs()
    assert len(runs) == 2
    assert (runs[0][2], runs[0][3]) == (1, 1)
    assert (runs[1][2], runs[1][3]) == (1, 0), "the second run added nobody"
    assert runs[1][4] == 1


def test_a_dry_run_records_nothing(book, tmp_path):
    """A dry run writes nothing, so it must not open a row it cannot close."""
    from typer.testing import CliRunner

    from navig_contacts.commands.contacts import app

    (tmp_path / "book").mkdir()
    path = _export(tmp_path / "book", [CARD, MATCH])

    result = CliRunner().invoke(app, ["import", str(path), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert _runs() == []


def test_a_contacts_json_import_records_a_run(book, tmp_path):
    """
    The address-book importer had the same gap: it wrote contacts and left no
    row, so the audit trail in this book jumps straight from 7 August to the
    vCard import.
    """
    from typer.testing import CliRunner

    from navig_contacts.commands.contacts import app

    path = tmp_path / "contacts.json"
    path.write_text(json.dumps({"contacts": {"list": [
        {"first_name": "Valeria", "last_name": "Sokolova",
         "phone_number": "+33678488684", "username": "lera"},
        {"first_name": "Nophone", "last_name": "Lead"},
    ]}}), encoding="utf-8")

    result = CliRunner().invoke(app, ["import", str(path)])
    assert result.exit_code == 0, result.output

    # Strict on purpose: if core's importer ever stops accepting this shape,
    # the run disappears and this must fail rather than skip past it.
    [run] = _runs()
    assert (run[0], run[1]) == ("telegram", "json")
    assert run[3] == 2, "both contacts added"
    assert run[4] == 0
    assert run[6], "finished_at set"


def test_a_dry_run_of_the_address_book_writes_no_run(book, tmp_path):
    from typer.testing import CliRunner

    from navig_contacts.commands.contacts import app

    path = tmp_path / "contacts.json"
    path.write_text(json.dumps({"contacts": {"list": [
        {"first_name": "Valeria", "last_name": "Sokolova",
         "phone_number": "+33678488684"},
    ]}}), encoding="utf-8")

    CliRunner().invoke(app, ["import", str(path), "--dry-run"])
    assert _runs() == []
