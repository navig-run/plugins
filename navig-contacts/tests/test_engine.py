"""The engine: phone validation, record linkage, tiers, idempotence, merges.

Every phone case here is a real value from the archive this was written against.
"""

from __future__ import annotations

import pytest
from conftest import write_vcf

from navig_contacts.store import HARD_KINDS
from navig_contacts.engine import db as db_mod
from navig_contacts.engine import vcf_reports
from navig_contacts.engine.identity import name_key, normalize_phone, repair_phone
from navig_contacts.engine.interests import ArchiveSplit
from navig_contacts.engine.merge import (
    absorb, build_clusters, split_identifier, undo_merge,
)
from navig_contacts.engine.vcard import parse_target
from navig_contacts.engine.vcf_import import import_vcf


# --- the phone gate --------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("+33678488684", "+33678488684"),
    ("+33 6 78 48 86 84", "+33678488684"),
    ("0613601584", "+33613601584"),
    ("+375295335919", "+375295335919"),
])
def test_valid_numbers_become_e164(raw, expected):
    assert normalize_phone(raw)[0] == expected


@pytest.mark.parametrize("raw,fragment", [
    ("33700", "too short"),            # French spam-reporting short code
    ("3949", "too short"),             # Pôle Emploi
    ("+666", "too short"),
    ("+7**********", "too short"),
    ("Google", "no digits"),
    ("+905313131313131", "not a valid number"),
    ("+7696969696969696969969999", "too long"),
])
def test_junk_is_rejected_with_a_reason(raw, fragment):
    number, reason = normalize_phone(raw)
    assert number is None
    assert fragment in reason


def test_doubled_country_code_is_repaired():
    assert repair_phone("+7+79603166750") == "+79603166750"
    assert normalize_phone("+7+79603166750")[0] == "+79603166750"


def test_a_region_is_only_ever_assumed_out_loud():
    assert normalize_phone("+33678488684")[1] == "explicit +"
    assert "assumed region" in normalize_phone("0613601584")[1]


# --- record linkage --------------------------------------------------------

def test_two_records_sharing_a_phone_are_one_person(tmp_path):
    path = write_vcf(tmp_path / "a.vcf", [
        "FN:Adnot Florian\nN:Adnot;Florian;;;\nTEL:+33678488684",
        "FN:Florian Adnot\nN:Adnot;Florian;;;\nTEL:+33 6 78 48 86 84",
    ])
    result = build_clusters(parse_target(path))
    assert len(result.clusters) == 1
    assert {a[0] for a in result.clusters[0].aliases()} == {
        "Adnot Florian", "Florian Adnot"}


def test_two_people_sharing_only_a_first_name_stay_separate(tmp_path):
    """The measured reason names are never a merge key."""
    path = write_vcf(tmp_path / "a.vcf", [
        "FN:Anna\nN:;Anna;;;\nTEL:+33678488684",
        "FN:Anna\nN:;Anna;;;\nTEL:+33610438304",
    ])
    assert len(build_clusters(parse_target(path)).clusters) == 2


def test_a_rejected_phone_merges_nobody(tmp_path):
    path = write_vcf(tmp_path / "a.vcf", [
        "FN:Person One\nN:One;Person;;;\nTEL:33700",
        "FN:Person Two\nN:Two;Person;;;\nTEL:33700",
    ])
    result = build_clusters(parse_target(path))
    assert len(result.clusters) == 2
    assert len(result.rejected) == 2


def test_the_schema_enforces_exactly_the_kinds_that_merge(book):
    """If the index and HARD_KINDS drift, merging breaks or two people share a number."""
    import sqlite3

    conn = sqlite3.connect(book)
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'idx_identifier_unique'"
    ).fetchone()[0]
    for kind in HARD_KINDS:
        assert f"'{kind}'" in sql
    assert "'url'" not in sql, "two people can share a company homepage"


def test_name_key_ignores_word_order_and_accents():
    assert name_key("Adnot Florian") == name_key("Florian Adnot")
    assert name_key("Sète") == name_key("Sete")


# --- tiers, persistence, idempotence ---------------------------------------

CARDS = [
    "FN:Florian Adnot\nN:Adnot;Florian;;;\nTEL:+33678488684\nEMAIL:f@example.com",
    "FN:Adnot Florian\nN:Adnot;Florian;;;\nTEL:+33 6 78 48 86 84",
    "FN:mailonly\nN:mailonly;;;;\nEMAIL:mail.only@example.com",
    "FN:handleonly\nN:handleonly;;;;\nX-SKYPE-USERNAME:handleonly",
    "FN:Linus Torvalds\nN:Torvalds;Linus;;;\n"
    "URL:http\\://www.google.com/profiles/102150693225130002912",
]


def test_tiers_and_that_the_archive_tier_is_never_stored(book, tmp_path):
    path = write_vcf(tmp_path / "a.vcf", CARDS)
    with db_mod.get_db() as conn:
        result = import_vcf(path, conn, "s1")

    counts = result.tier_counts()
    assert (counts["verified"], counts["email"], counts["handle"],
            counts["archive"]) == (1, 1, 1, 1)

    with db_mod.get_db() as conn:
        names = [r[0] for r in conn.execute("SELECT display_name FROM contacts")]
    assert "Linus Torvalds" not in names, "no phone, no email, no handle"
    assert len(names) == 3


def test_an_import_writes_routes_so_dispatch_can_reach_the_people(book, tmp_path):
    """
    The point of one store: importing an address book makes those people
    messageable, instead of the import being a read-only record beside the
    routing table.
    """
    from navig_contacts.store import ContactStore

    path = write_vcf(tmp_path / "a.vcf", CARDS)
    with db_mod.get_db() as conn:
        import_vcf(path, conn, "s1")

    store = ContactStore(db_path=book)
    contact = store.resolve_alias("p:33678488684")
    assert contact is not None, "an imported person must be resolvable by dispatch"
    assert ("sms", "+33678488684") in {(r.network, r.address) for r in contact.routes}
    assert ("email", "f@example.com") in {(r.network, r.address) for r in contact.routes}

    # A skype handle is not a transport navig can send on, so it stays an
    # identifier and produces no route.
    handle = store.resolve_alias("s:handleonly")
    assert handle is not None
    assert handle.routes == []


def test_importing_twice_adds_nobody(book, tmp_path):
    path = write_vcf(tmp_path / "a.vcf", CARDS)
    with db_mod.get_db() as conn:
        first = import_vcf(path, conn, "s1")
    with db_mod.get_db() as conn:
        second = import_vcf(path, conn, "s2")

    assert first.summary.inserted == 3
    assert second.summary.inserted == 0
    assert second.summary.duplicates == 3
    with db_mod.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 3
        # Florian: sms + email. mailonly: email. handleonly: skype is not a
        # transport navig sends on, so it makes no route.
        assert conn.execute("SELECT COUNT(*) FROM contact_routes").fetchone()[0] == 3


def test_a_phone_promotes_a_lead(book, tmp_path):
    first = write_vcf(tmp_path / "one.vcf",
                      ["FN:handleonly\nN:handleonly;;;;\nX-SKYPE-USERNAME:handleonly"])
    with db_mod.get_db() as conn:
        import_vcf(first, conn, "s1")
        assert conn.execute(
            "SELECT tier FROM contacts WHERE alias='s:handleonly'").fetchone()[0] == "handle"

    second = write_vcf(tmp_path / "two.vcf", [
        "FN:Handle Person\nN:Person;Handle;;;\n"
        "X-SKYPE-USERNAME:handleonly\nTEL:+33678488684"])
    with db_mod.get_db() as conn:
        import_vcf(second, conn, "s2")
        row = conn.execute(
            "SELECT tier, primary_phone FROM contacts WHERE alias='s:handleonly'"
        ).fetchone()
        assert conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 1
    assert row["tier"] == "verified"
    assert row["primary_phone"] == "+33678488684"


def test_an_import_does_not_disturb_a_hand_made_routing_contact(book, tmp_path):
    """The routing half predates the address book and must survive an import."""
    from navig_contacts.store import ContactStore

    store = ContactStore(db_path=book)
    store.add_contact(alias="alice", display_name="Alice B.",
                      routes=["whatsapp:+33600000000"], default_network="whatsapp")

    path = write_vcf(tmp_path / "a.vcf", CARDS)
    with db_mod.get_db() as conn:
        import_vcf(path, conn, "s1")

    alice = ContactStore(db_path=book).resolve_alias("alice")
    assert alice.display_name == "Alice B."
    assert alice.default_network == "whatsapp"
    assert [(r.network, r.address) for r in alice.routes] == [
        ("whatsapp", "+33600000000")]


def test_an_import_attaches_to_a_contact_you_added_by_hand(book, tmp_path):
    """
    A hand-made contact has a ROUTE and no identifier row. An importer that only
    looked at identifiers would file the same person again under a different
    alias, with the number on both — the exact duplication one store is meant to
    end.
    """
    from navig_contacts.store import ContactStore

    ContactStore(db_path=book).add_contact(
        alias="alice", display_name="Alice B.", routes=["sms:+33678488684"])

    path = write_vcf(tmp_path / "a.vcf", ["FN:Alice Bernard" + chr(10) + "N:Bernard;Alice;;;" + chr(10) + "TEL:+33678488684"])
    with db_mod.get_db() as conn:
        result = import_vcf(path, conn, "s1")
        rows = conn.execute(
            "SELECT alias, display_name, primary_phone, tier FROM contacts").fetchall()
        aliases = [a[0] for a in conn.execute("SELECT name FROM contact_aliases")]

    assert result.summary.inserted == 0
    assert result.summary.duplicates == 1
    assert len(rows) == 1, "the import must not file her a second time"
    assert rows[0]["alias"] == "alice", "her chosen alias survives the import"
    assert rows[0]["display_name"] == "Alice B."
    assert rows[0]["primary_phone"] == "+33678488684"
    assert rows[0]["tier"] == "verified", "a validated phone promotes her"
    assert "Alice Bernard" in aliases, "the imported spelling is kept as an alias"


# --- merges and splits are reversible --------------------------------------

def _two_people(tmp_path):
    return write_vcf(tmp_path / "a.vcf", [
        "FN:Person One\nN:One;Person;;;\nTEL:+33678488684",
        "FN:Person Two\nN:Two;Person;;;\nTEL:+33610438304",
    ])


def test_merge_then_undo_restores_the_contact(book, tmp_path):
    with db_mod.get_db() as conn:
        import_vcf(_two_people(tmp_path), conn, "s1")
        first = conn.execute(
            "SELECT id FROM contacts WHERE alias='p:33678488684'").fetchone()[0]
        second = conn.execute(
            "SELECT id FROM contacts WHERE alias='p:33610438304'").fetchone()[0]
        absorb(conn, first, second, "s1", "for the test")
        merge_id = conn.execute("SELECT id FROM merge_log").fetchone()[0]
        assert conn.execute(
            "SELECT contact_id FROM contact_identifiers "
            "WHERE value_norm='+33610438304'").fetchone()[0] == first

        outcome = undo_merge(conn, merge_id)
        assert outcome.status == "ok"
        restored = conn.execute(
            "SELECT merged_into, is_deleted FROM contacts WHERE id=?", (second,)
        ).fetchone()
        assert restored["merged_into"] is None
        assert restored["is_deleted"] == 0
        assert conn.execute(
            "SELECT contact_id FROM contact_identifiers "
            "WHERE value_norm='+33610438304'").fetchone()[0] == second


def test_undoing_twice_is_refused(book, tmp_path):
    with db_mod.get_db() as conn:
        import_vcf(_two_people(tmp_path), conn, "s1")
        a = conn.execute("SELECT id FROM contacts WHERE alias='p:33678488684'"
                         ).fetchone()[0]
        b = conn.execute("SELECT id FROM contacts WHERE alias='p:33610438304'"
                         ).fetchone()[0]
        absorb(conn, a, b, "s1", "for the test")
        merge_id = conn.execute("SELECT id FROM merge_log").fetchone()[0]
        assert undo_merge(conn, merge_id).status == "ok"
        assert undo_merge(conn, merge_id).status == "already_undone"
        assert undo_merge(conn, 9999).status == "missing"


def test_split_moves_an_identifier_and_its_route(book, tmp_path):
    """
    The real over-merge: one record listed somebody else's number. Splitting
    must take the route with the identifier, or the number stays messageable
    on the wrong contact.
    """
    path = write_vcf(tmp_path / "a.vcf", [
        "FN:Kirill\nN:Kirill;;;;\nTEL:+33467695457\nTEL:+33671514812",
    ])
    with db_mod.get_db() as conn:
        import_vcf(path, conn, "s1")
        outcome = split_identifier(conn, "p:33467695457",
                                   "phone:+33671514812", "Kirill mama")
        assert outcome.status == "ok"
        assert outcome.detail == "p:33671514812"

        mother = conn.execute(
            "SELECT id, display_name, tier, primary_phone FROM contacts "
            "WHERE alias='p:33671514812'").fetchone()
        assert mother["display_name"] == "Kirill mama"
        assert mother["primary_phone"] == "+33671514812"
        assert conn.execute(
            "SELECT contact_id FROM contact_routes WHERE address='+33671514812'"
        ).fetchone()[0] == mother["id"]


def test_split_refuses_to_strand_a_contact(book, tmp_path):
    path = write_vcf(tmp_path / "a.vcf",
                     ["FN:Only One\nN:One;Only;;;\nTEL:+33678488684"])
    with db_mod.get_db() as conn:
        import_vcf(path, conn, "s1")
        outcome = split_identifier(conn, "p:33678488684", "phone:+33678488684")
    assert outcome.status == "would_strand"


def test_split_reports_a_bad_spec_and_an_identifier_not_held(book, tmp_path):
    with db_mod.get_db() as conn:
        import_vcf(_two_people(tmp_path), conn, "s1")
        assert split_identifier(conn, "p:33678488684", "nonsense").status == "bad_spec"
        assert split_identifier(
            conn, "p:33678488684", "phone:+33000000000").status == "not_held"


# ---------------------------------------------------------------------------
# Report filenames carry the LOCAL day
# ---------------------------------------------------------------------------

def test_report_filenames_use_the_local_day_not_utc(monkeypatch, tmp_path):
    """A UTC day is a different day for hours each night.

    Run the import at 21:00 in UTC+3 and every report is stamped tomorrow, so the
    file a human goes looking for is not in the listing under today's date. The two
    clocks are forced to DISAGREE here so a revert to UTC fails every day of the
    year, rather than only during the window where it happens to matter.
    """
    from datetime import date as real_date, datetime as real_datetime, timezone

    local_day = real_date(2026, 3, 14)
    utc_instant = real_datetime(2026, 3, 15, 1, 30, tzinfo=timezone.utc)

    class FrozenDate(real_date):
        @classmethod
        def today(cls):
            return local_day

    class FrozenDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return utc_instant

    monkeypatch.setattr(vcf_reports, "date", FrozenDate)
    monkeypatch.setattr(vcf_reports, "datetime", FrozenDatetime)

    written = vcf_reports.write_all(tmp_path, [], [], [], ArchiveSplit())

    for label, path in written.items():
        assert "2026-03-14" in path.name, (
            f"{label} is stamped {path.name} — that is the UTC day, not the local one "
            f"the user's calendar shows"
        )
        assert "2026-03-15" not in path.name

    # The instant is a different question and stays UTC — it says so in the string.
    assert "2026-03-15 01:30 UTC" in written["merges"].read_text(encoding="utf-8")


# --- a human's correction must outlive the next import ----------------------

def test_a_re_import_does_not_undo_a_split(book, tmp_path):
    """
    Regression: it did. A split is the correction an automatic import cannot
    make for itself — one record listing somebody else's number. The next run
    re-read the same source, found the same shared number, and merged the two
    back together, wiping the verdict a human had just made.
    """
    path = write_vcf(tmp_path / "a.vcf",
                     ["FN:Kirill" + chr(10) + "N:Kirill;;;;" + chr(10)
                      + "TEL:+33467695457" + chr(10) + "TEL:+33671514812"])
    with db_mod.get_db() as conn:
        import_vcf(path, conn, "s1")
        assert split_identifier(conn, "p:33467695457", "phone:+33671514812",
                                "Kirill mama").status == "ok"

    with db_mod.get_db() as conn:
        import_vcf(path, conn, "s2")
        state = {r["alias"]: r["merged_into"] for r in conn.execute(
            "SELECT alias, merged_into FROM contacts "
            "WHERE alias IN ('p:33467695457', 'p:33671514812')")}
        owners = dict(conn.execute(
            "SELECT i.value_norm, c.alias FROM contact_identifiers i "
            "JOIN contacts c ON c.id = i.contact_id "
            "WHERE i.value_norm IN ('+33467695457', '+33671514812')"))

    assert state == {"p:33467695457": None, "p:33671514812": None}, (
        "the re-import merged the two people back together")
    assert owners["+33671514812"] == "p:33671514812", "her number stayed hers"
    assert owners["+33467695457"] == "p:33467695457", "his number stayed his"


def test_a_reviewed_merge_drops_out_of_the_report(book, tmp_path):
    from navig_contacts.engine.merge import currently_flagged
    from navig_contacts.engine.vcf_reports import suspicious_clusters

    path = write_vcf(tmp_path / "a.vcf", [
        "FN:Amandine Brault" + chr(10) + "N:Brault;Amandine;;;" + chr(10)
        + "TEL:+33652434297",
        "FN:Ken Wood" + chr(10) + "N:Wood;Ken;;;" + chr(10) + "TEL:+33652434297",
    ])
    with db_mod.get_db() as conn:
        result = import_vcf(path, conn, "s1")
        flagged = currently_flagged(conn)
        assert flagged == ["p:33652434297"], "two unrelated names, one mobile"
        assert len(suspicious_clusters(result.clusters)) == 1

        conn.execute("UPDATE contacts SET merge_reviewed_at = datetime('now') "
                     "WHERE alias = ?", ("p:33652434297",))
        assert currently_flagged(conn) == []
        assert suspicious_clusters(result.clusters, {"p:33652434297"}) == []


def test_splitting_puts_a_contact_back_on_the_review_list(book, tmp_path):
    """A split says the earlier "one person" verdict was wrong."""
    path = write_vcf(tmp_path / "a.vcf",
                     ["FN:Kirill" + chr(10) + "N:Kirill;;;;" + chr(10)
                      + "TEL:+33467695457" + chr(10) + "TEL:+33671514812"])
    with db_mod.get_db() as conn:
        import_vcf(path, conn, "s1")
        conn.execute("UPDATE contacts SET merge_reviewed_at = datetime('now') "
                     "WHERE alias = ?", ("p:33467695457",))
        split_identifier(conn, "p:33467695457", "phone:+33671514812", "Kirill mama")
        assert conn.execute(
            "SELECT merge_reviewed_at FROM contacts WHERE alias = ?",
            ("p:33467695457",)).fetchone()[0] is None
