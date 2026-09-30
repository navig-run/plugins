"""
Migrating a book from before the two halves merged.

Every case here came from migrating a real 4,077-contact book and finding what
went wrong.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from navig_contacts.store import ContactStore
from navig_contacts.engine.migrate import MigrationError, inspect, migrate

#: The pre-merge space-local schema, as it actually shipped.
PREMERGE = """
CREATE TABLE contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT UNIQUE NOT NULL, full_name TEXT, gender TEXT DEFAULT 'unknown',
    language TEXT, country TEXT, city TEXT, photo_path TEXT,
    source_profile TEXT, is_deleted INTEGER DEFAULT 0,
    created_at DATETIME, updated_at DATETIME,
    tier TEXT, primary_phone TEXT, primary_email TEXT, birthday TEXT,
    org TEXT, job_title TEXT, is_org INTEGER DEFAULT 0, merged_into INTEGER
);
CREATE TABLE social_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    platform TEXT, handle TEXT, profile_url TEXT, is_banned INTEGER DEFAULT 0,
    resolve_state TEXT, resolve_checked_at TEXT
);
CREATE TABLE contact_status (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    ever_talked INTEGER DEFAULT 0, notes TEXT
);
CREATE TABLE import_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, source TEXT,
    input_path TEXT, kind TEXT, started_at DATETIME, finished_at DATETIME,
    max_contact_id_before INTEGER, max_contact_id_after INTEGER,
    total INTEGER DEFAULT 0, inserted INTEGER DEFAULT 0,
    duplicates INTEGER DEFAULT 0, errors INTEGER DEFAULT 0,
    photos_matched INTEGER DEFAULT 0, photos_unmatched INTEGER DEFAULT 0
);
CREATE TABLE contact_identifiers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    kind TEXT NOT NULL, value_norm TEXT NOT NULL, value_raw TEXT,
    is_primary INTEGER DEFAULT 0, source_file TEXT, created_at DATETIME
);
CREATE TABLE contact_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    name TEXT NOT NULL, name_key TEXT, source_file TEXT,
    is_primary INTEGER DEFAULT 0, created_at DATETIME
);
CREATE TABLE contact_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    source_file TEXT, card_index INTEGER, raw_card TEXT, imported_at DATETIME
);
CREATE TABLE merge_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, surviving_id INTEGER,
    merged_uid TEXT, reason TEXT, key_kind TEXT, key_value TEXT,
    payload TEXT, undone_at DATETIME, created_at DATETIME
);
"""


@pytest.fixture()
def premerge(tmp_path):
    """A book in the shape the older space-local tool left behind."""
    path = tmp_path / "contacts.db"
    conn = sqlite3.connect(path)
    conn.executescript(PREMERGE)
    conn.execute(
        "INSERT INTO contacts (uid, full_name, tier, primary_phone, city) "
        "VALUES ('p:33678488684', 'Florian Adnot', 'verified', '+33678488684', 'Sète')")
    conn.execute(
        "INSERT INTO contacts (uid, full_name, tier) VALUES ('totara04', NULL, 'handle')")
    conn.execute(
        "INSERT INTO contact_identifiers (contact_id, kind, value_norm) "
        "VALUES (1, 'phone', '+33678488684')")
    conn.execute(
        "INSERT INTO social_profiles (contact_id, platform, handle, "
        "resolve_state, resolve_checked_at) "
        "VALUES (2, 'telegram', 'Totara04', 'gone', '2026-09-05 16:08:13')")
    conn.execute("INSERT INTO contact_status (contact_id, notes) "
                 "VALUES (1, 'Age at import: 25')")
    conn.execute("INSERT INTO import_runs (source, kind, total) "
                 "VALUES ('davinchik', 'json', 222)")
    conn.commit()
    conn.close()
    return path


def test_inspect_recognises_a_premerge_book(premerge):
    plan = inspect(premerge)
    assert plan.needed
    assert plan.contacts == 2
    assert plan.handles == 1
    assert plan.notes == 1
    assert plan.runs == 1
    assert any("rebuild contacts" in s for s in plan.steps)


def test_migration_carries_everything_across(premerge):
    migrate(premerge)
    conn = sqlite3.connect(premerge)
    conn.row_factory = sqlite3.Row

    rows = {r["alias"]: r for r in conn.execute("SELECT * FROM contacts")}
    assert set(rows) == {"p:33678488684", "totara04"}
    assert rows["p:33678488684"]["display_name"] == "Florian Adnot"
    assert rows["p:33678488684"]["city"] == "Sète"
    assert rows["p:33678488684"]["notes"] == "Age at import: 25"

    handles = conn.execute(
        "SELECT kind, value_norm FROM contact_identifiers WHERE kind='telegram'"
    ).fetchall()
    assert [tuple(h) for h in handles] == [("telegram", "totara04")]
    assert conn.execute("SELECT COUNT(*) FROM contact_import_runs").fetchone()[0] == 1


def test_a_null_name_becomes_an_empty_string_not_a_null(premerge):
    """core declares display_name NOT NULL DEFAULT ''; a rename would keep NULLs."""
    migrate(premerge)
    conn = sqlite3.connect(premerge)
    assert conn.execute(
        "SELECT COUNT(*) FROM contacts WHERE display_name IS NULL").fetchone()[0] == 0
    assert conn.execute(
        "SELECT display_name FROM contacts WHERE alias='totara04'").fetchone()[0] == ""


def test_alias_becomes_case_insensitive(premerge):
    """
    The reason contacts is rebuilt rather than renamed. Core queries
    `WHERE alias = ? COLLATE NOCASE`; without the collation on the column, the
    UNIQUE constraint is case-sensitive and two rows can both answer.
    """
    migrate(premerge)
    conn = sqlite3.connect(premerge)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO contacts (alias, display_name) "
                     "VALUES ('TOTARA04', 'a second Totara')")


def test_identifiers_become_routes_dispatch_can_use(premerge):
    migrate(premerge)
    store = ContactStore(db_path=premerge)
    florian = store.resolve_alias("p:33678488684")
    assert ("sms", "+33678488684") in {(r.network, r.address) for r in florian.routes}
    totara = store.resolve_alias("totara04")
    assert ("telegram", "totara04") in {(r.network, r.address) for r in totara.routes}


def test_superseded_tables_are_dropped_only_after_the_copy(premerge):
    migrate(premerge)
    conn = sqlite3.connect(premerge)
    left = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN ('social_profiles', 'contact_status', 'import_runs')")]
    assert left == []


def test_an_interrupted_earlier_open_does_not_strand_the_import_log(premerge):
    """
    A failed open of a pre-merge book can leave an EMPTY contact_import_runs
    behind. A migration that renamed import_runs would then either fail or skip,
    stranding the history — which is exactly what happened on the real book.
    """
    conn = sqlite3.connect(premerge)
    conn.execute("CREATE TABLE contact_import_runs (id INTEGER PRIMARY KEY, "
                 "session_id TEXT, source TEXT, kind TEXT, total INTEGER)")
    conn.commit()
    conn.close()

    migrate(premerge)
    conn = sqlite3.connect(premerge)
    assert conn.execute("SELECT COUNT(*) FROM contact_import_runs").fetchone()[0] == 1
    assert conn.execute(
        "SELECT source FROM contact_import_runs").fetchone()[0] == "davinchik"


def test_migrating_twice_is_a_no_op(premerge):
    migrate(premerge)
    before = sqlite3.connect(premerge).execute(
        "SELECT COUNT(*) FROM contacts").fetchone()[0]
    plan = migrate(premerge)
    assert not plan.needed
    assert plan.reason == "already on the shared schema"
    assert sqlite3.connect(premerge).execute(
        "SELECT COUNT(*) FROM contacts").fetchone()[0] == before


def test_a_book_already_on_the_shared_schema_is_left_alone(tmp_path):
    path = tmp_path / "contacts.db"
    ContactStore(db_path=path).add_contact(alias="alice", display_name="Alice")
    plan = inspect(path)
    assert not plan.needed
    assert plan.reason == "already on the shared schema"


def test_an_unrecognised_book_is_left_alone(tmp_path):
    path = tmp_path / "other.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE contacts (id INTEGER PRIMARY KEY, whatever TEXT)")
    conn.commit()
    conn.close()
    plan = inspect(path)
    assert not plan.needed
    assert "unrecognised" in plan.reason


def test_a_missing_book_is_not_a_crash(tmp_path):
    plan = inspect(tmp_path / "nothing.db")
    assert not plan.needed
    assert "no book" in plan.reason


def test_a_row_count_mismatch_aborts_with_the_book_intact(premerge, monkeypatch):
    """The drop must never run on an unverified copy."""
    import navig_contacts.engine.migrate as mod

    monkeypatch.setattr(mod, "_verify", lambda conn, plan: (_ for _ in ()).throw(
        MigrationError("pretend the copy came up short")))
    with pytest.raises(MigrationError):
        migrate(premerge)

    conn = sqlite3.connect(premerge)
    assert conn.execute("SELECT COUNT(*) FROM social_profiles").fetchone()[0] == 1, (
        "the superseded tables must survive a failed migration")
    assert "uid" in [r[1] for r in conn.execute("PRAGMA table_info(contacts)")], (
        "the book must still be readable by the tool that wrote it")


def test_the_rebuild_leaves_a_book_that_can_still_be_written_to(premerge):
    """
    Regression, found on the real migrated book: it was read-only and nothing
    said so.

    Since 3.25 sqlite rewrites every *other* table's foreign key to follow a
    renamed table, so renaming `contacts` out of the way repointed all four
    child tables at `_premerge_contacts`, and dropping it left them referencing
    a table that does not exist. Sqlite ships with foreign keys off, so the
    book read perfectly -- and every write failed the moment anything turned
    them on, which is exactly how `navig contacts` opens it.
    """
    migrate(premerge)

    conn = sqlite3.connect(premerge)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    for name in sorted(tables):
        for row in conn.execute(f'PRAGMA foreign_key_list("{name}")'):
            assert row[2] in tables, f"{name} references {row[2]}, which is gone"

    conn.execute("PRAGMA foreign_keys = ON")
    contact_id = conn.execute("SELECT id FROM contacts").fetchone()[0]
    for table, sql in (
        ("contact_aliases", "(contact_id, name) VALUES (?, 'a new alias')"),
        ("contact_routes",
         "(contact_id, network, address) VALUES (?, 'sms', '+33600000000')"),
        ("contact_identifiers",
         "(contact_id, kind, value_norm) VALUES (?, 'phone', '+33600000000')"),
    ):
        conn.execute(f"INSERT INTO {table} {sql}", (contact_id,))


def test_the_handle_liveness_verdicts_are_carried_onto_the_routes(premerge):
    """
    A verdict costs a lookup to produce, and `export` excludes dead handles by
    default -- so dropping `social_profiles` without them would silently make
    every dead handle look never-checked again.

    A route is a transport, so "this transport no longer resolves" is a fact
    about the route, not the person.
    """
    migrate(premerge)
    conn = sqlite3.connect(premerge)
    meta = conn.execute(
        "SELECT meta_json FROM contact_routes "
        "WHERE network = 'telegram' AND address = 'totara04'").fetchone()[0]
    assert json.loads(meta) == {"resolve_state": "gone",
                                "resolve_checked_at": "2026-09-05 16:08:13"}

    # A handle that was never checked gets no verdict invented for it.
    for (other,) in conn.execute(
            "SELECT COALESCE(meta_json, '') FROM contact_routes "
            "WHERE network <> 'telegram'"):
        assert "resolve_state" not in other
