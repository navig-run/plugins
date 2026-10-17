"""
The consistency check.

Every case here is a disagreement this book actually had. None of them raise —
they sit there being wrong — which is the whole reason for a command that goes
and looks.
"""

from __future__ import annotations

import sqlite3

import pytest

from navig_contacts.store import ContactStore
from navig_contacts.engine import db as db_mod
from navig_contacts.engine.doctor import diagnose, repair


def _checks(conn) -> dict:
    return {f.check: f for f in diagnose(conn)}


def test_a_clean_book_says_so(book):
    ContactStore(db_path=book).add_contact(
        alias="alice", display_name="Alice", routes=["sms:+33612345678"])
    with db_mod.get_db() as conn:
        assert diagnose(conn) == []


def test_a_route_with_no_identifier_is_reported_and_repaired(book):
    """What let an address-book import file the same person twice."""
    store = ContactStore(db_path=book)
    store.add_contact(alias="alice", routes=["sms:+33612345678"])
    with db_mod.get_db() as conn:
        conn.execute("DELETE FROM contact_identifiers")   # the pre-fix state
        found = _checks(conn)
        assert "routes with no identifier" in found
        assert found["routes with no identifier"].count == 1

        assert repair(conn)["identifiers_added"] == 1
        assert [tuple(r) for r in conn.execute(
            "SELECT kind, value_norm FROM contact_identifiers")] == [
                ("phone", "+33612345678")]
        assert "routes with no identifier" not in _checks(conn)


def test_an_identifier_with_no_route_is_reported_and_repaired(book):
    """You know how to reach them; dispatch does not."""
    store = ContactStore(db_path=book)
    store.add_contact(alias="alice")
    with db_mod.get_db() as conn:
        conn.execute(
            "INSERT INTO contact_identifiers (contact_id, kind, value_norm) "
            "SELECT id, 'phone', '+33612345678' FROM contacts WHERE alias='alice'")
        assert "phone identifiers with no sms route" in _checks(conn)

        assert repair(conn)["routes_added"] >= 1
        assert [tuple(r) for r in conn.execute(
            "SELECT network, address FROM contact_routes")] == [
                ("sms", "+33612345678")]


def test_one_address_on_two_contacts_is_reported_but_not_touched(book):
    """
    A household landline is legitimate; two records of one person are not.
    Only a human can tell which, so the check reports and repairs nothing.
    """
    store = ContactStore(db_path=book)
    store.add_contact(alias="alice", routes=["sms:+33612345678"])
    store.add_contact(alias="bob", routes=["sms:+33612345678"])
    with db_mod.get_db() as conn:
        found = _checks(conn)
        assert "one address, several contacts" in found
        assert not found["one address, several contacts"].fix, (
            "deciding they are one person is a merge, not a repair")
        repair(conn)
        assert "one address, several contacts" in _checks(conn)


def test_a_route_left_on_a_merged_contact_is_moved(book):
    """A message sent there reaches a record nothing points at."""
    store = ContactStore(db_path=book)
    store.add_contact(alias="alice")
    store.add_contact(alias="alice_old", routes=["telegram:alice_tg"])
    with db_mod.get_db() as conn:
        survivor = conn.execute(
            "SELECT id FROM contacts WHERE alias='alice'").fetchone()[0]
        conn.execute("UPDATE contacts SET merged_into = ?, is_deleted = 1 "
                     "WHERE alias = 'alice_old'", (survivor,))
        assert "routes on a merged or deleted contact" in _checks(conn)

        repair(conn)
        assert conn.execute(
            "SELECT contact_id FROM contact_routes").fetchone()[0] == survivor


def test_orphaned_evidence_is_reported_and_removed(book):
    """
    A contact deleted with the foreign keys off — which is how a hand-run
    `DELETE FROM contacts` in sqlite3 leaves a book, since sqlite ships with
    enforcement off by default.
    """
    store = ContactStore(db_path=book)
    store.add_contact(alias="ghost", display_name="Ghost")
    with db_mod.get_db() as conn:
        # Before any DML: sqlite ignores this pragma inside a transaction, and
        # python's sqlite3 opens one on the first write.
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "INSERT INTO contact_aliases (contact_id, name) "
            "SELECT id, 'Ghost' FROM contacts WHERE alias = 'ghost'")
        conn.execute("DELETE FROM contacts WHERE alias = 'ghost'")
        assert "orphaned contact_aliases" in _checks(conn)
        assert repair(conn)["orphans_removed"] >= 1
        assert "orphaned contact_aliases" not in _checks(conn)


def test_a_tier_that_contradicts_the_identifiers_is_corrected(book):
    """`tier` is the model — a tier that disagrees is the model lying."""
    store = ContactStore(db_path=book)
    store.add_contact(alias="alice", routes=["sms:+33612345678"])
    with db_mod.get_db() as conn:
        conn.execute("UPDATE contacts SET tier = 'handle' WHERE alias = 'alice'")
        assert "tier disagrees with the identifiers" in _checks(conn)

        assert repair(conn)["tiers_corrected"] == 1
        assert conn.execute(
            "SELECT tier FROM contacts WHERE alias='alice'").fetchone()[0] == "verified"


def test_an_empty_premerge_table_is_dropped(book):
    """
    The retired space-local tool rebuilds its own schema on every run, so one
    invocation against a migrated book puts these back, empty.
    """
    with db_mod.get_db() as conn:
        conn.execute("CREATE TABLE social_profiles (id INTEGER PRIMARY KEY, "
                     "contact_id INTEGER, platform TEXT, handle TEXT)")
        found = _checks(conn)
        assert "empty tables from before the merge" in found

        assert repair(conn)["empty_premerge_tables_dropped"] == 1
        assert "empty tables from before the merge" not in _checks(conn)


def test_a_premerge_table_WITH_rows_is_never_dropped(book):
    """
    Rows in there are data that has not been carried across. Dropping it is the
    one unrecoverable thing this command could do, so it refuses and says
    `migrate` instead.
    """
    with db_mod.get_db() as conn:
        conn.execute("CREATE TABLE social_profiles (id INTEGER PRIMARY KEY, "
                     "contact_id INTEGER, platform TEXT, handle TEXT)")
        conn.execute("INSERT INTO social_profiles (contact_id, platform, handle) "
                     "VALUES (1, 'telegram', 'alice')")
        found = _checks(conn)
        assert "tables from before the merge, still holding rows" in found
        assert found["tables from before the merge, still holding rows"].fix == (
            "navig contacts migrate --apply")

        repair(conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM social_profiles").fetchone()[0] == 1


def test_a_migration_that_repointed_the_foreign_keys_is_found_and_rebuilt(book):
    """
    Regression, found on the real migrated book.

    Since 3.25 sqlite rewrites every *other* table's foreign key to follow a
    renamed table. `migrate` renamed `contacts` out of the way to rebuild it,
    which silently repointed its four child tables at the temporary name, and
    dropping it left them referencing a table that does not exist.

    Nothing complained: sqlite ships with foreign keys off, so the book read
    perfectly. It only broke on the first write with them on -- which is how
    `navig contacts` opens the book -- and the whole address book was
    effectively read-only.
    """
    store = ContactStore(db_path=book)
    store.add_contact(alias="alice", display_name="Alice",
                      routes=["sms:+33612345678"])

    with db_mod.get_db() as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("PRAGMA legacy_alter_table = OFF")
        # What `migrate` did: rename out of the way, build the new table, copy,
        # drop the old one.
        conn.execute("ALTER TABLE contacts RENAME TO _premerge_contacts")
        conn.execute(
            "CREATE TABLE contacts (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "alias TEXT NOT NULL UNIQUE COLLATE NOCASE, "
            "display_name TEXT NOT NULL DEFAULT '', tier TEXT, "
            "is_deleted INTEGER DEFAULT 0, merged_into INTEGER)")
        conn.execute("INSERT INTO contacts (id, alias, display_name, tier) "
                     "SELECT id, alias, display_name, tier FROM _premerge_contacts")
        conn.execute("DROP TABLE _premerge_contacts")

    # The four child tables now reference a table that is not there.
    with db_mod.get_db() as conn:
        found = _checks(conn)
        assert "foreign keys pointing at a table that is gone" in found
        assert found["foreign keys pointing at a table that is gone"].count == 4

        with pytest.raises(sqlite3.OperationalError, match="_premerge_contacts"):
            conn.execute(
                "INSERT INTO contact_aliases (contact_id, name) VALUES (1, 'x')")

    with db_mod.get_db() as conn:
        assert repair(conn)["foreign_keys_repointed"] == 4
        assert diagnose(conn) == []

    with db_mod.get_db() as conn:
        # The rows survived, and the book takes writes again.
        assert conn.execute(
            "SELECT COUNT(*) FROM contact_identifiers").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM contact_routes").fetchone()[0] == 1
        contact_id = conn.execute("SELECT id FROM contacts").fetchone()[0]
        conn.execute("INSERT INTO contact_aliases (contact_id, name) VALUES (?, 'Al')",
                     (contact_id,))

    # And the merge key -- the partial unique index -- came back with the table.
    with db_mod.get_db() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO contact_identifiers (contact_id, kind, value_norm) "
                "VALUES (?, 'phone', '+33612345678')", (contact_id,))
