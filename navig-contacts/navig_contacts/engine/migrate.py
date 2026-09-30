"""
Bringing a pre-merge contact book up to the shared schema.

Before ``navig contacts`` was one command it was two, and a space that ran the
older space-local tool has a book whose ``contacts`` table is keyed on ``uid``
with a ``full_name``, with Telegram handles in ``social_profiles``, notes in
``contact_status`` and an ``import_runs`` log. The merged store keys on
``alias`` with a ``display_name``, and a handle is an identifier plus a route.

The ``contacts`` table is **rebuilt**, not renamed column by column. Renaming
gets the names right and the *constraints* wrong: core declares
``alias TEXT NOT NULL UNIQUE COLLATE NOCASE`` and ``display_name TEXT NOT NULL
DEFAULT ''``, and SQLite cannot add a collation to an existing column. A book
migrated by rename would accept ``Alice`` and ``alice`` as two contacts, and
then ``resolve_alias`` — which queries ``COLLATE NOCASE`` — would match both.

Everything is copied before anything is dropped, and the counts are checked
after the copy; a mismatch aborts the transaction with the book untouched.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

#: Tables the pre-merge book kept beside `contacts`, whose contents move into
#: the shared schema. Dropped only after every row has been copied and counted.
SUPERSEDED = ("social_profiles", "contact_status", "import_runs")


@dataclass
class MigrationPlan:
    """What migrating this book would do."""

    needed: bool = False
    reason: str = ""
    contacts: int = 0
    handles: int = 0
    notes: int = 0
    runs: int = 0
    routes_to_create: int = 0
    steps: list[str] = field(default_factory=list)


class MigrationError(RuntimeError):
    """The migration refused to finish, and changed nothing."""


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    except sqlite3.Error:
        return []


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone())


def _count(conn: sqlite3.Connection, table: str) -> int:
    if not _has_table(conn, table):
        return 0
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def inspect(path: Path) -> MigrationPlan:
    """Decide whether *path* is a pre-merge book, without changing it."""
    plan = MigrationPlan()
    if not path.exists():
        plan.reason = "no book at that path yet"
        return plan

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    try:
        if not _has_table(conn, "contacts"):
            plan.reason = "no contacts table — nothing to migrate"
            return plan
        cols = _columns(conn, "contacts")
        if "alias" in cols:
            plan.reason = "already on the shared schema"
            return plan
        if "uid" not in cols:
            plan.reason = "unrecognised contacts table — leave it alone"
            return plan

        plan.needed = True
        plan.reason = "a pre-merge book: contacts.uid, handles in social_profiles"
        plan.contacts = _count(conn, "contacts")
        plan.handles = conn.execute(
            "SELECT COUNT(*) FROM social_profiles WHERE handle IS NOT NULL "
            "AND TRIM(handle) <> ''").fetchone()[0] if _has_table(
                conn, "social_profiles") else 0
        plan.notes = conn.execute(
            "SELECT COUNT(*) FROM contact_status WHERE notes IS NOT NULL "
            "AND TRIM(notes) <> ''").fetchone()[0] if _has_table(
                conn, "contact_status") else 0
        plan.runs = _count(conn, "import_runs")
        plan.routes_to_create = conn.execute(
            "SELECT COUNT(*) FROM contact_identifiers "
            "WHERE kind IN ('phone', 'email', 'telegram')").fetchone()[0] if _has_table(
                conn, "contact_identifiers") else 0

        plan.steps.append(
            f"rebuild contacts ({plan.contacts} rows) on the shared schema — "
            f"uid becomes a case-insensitive alias, full_name becomes display_name")
        if plan.handles:
            plan.steps.append(f"turn {plan.handles} social profiles into identifiers")
        if plan.routes_to_create:
            plan.steps.append(
                f"give {plan.routes_to_create} identifiers a route, so dispatch "
                f"can reach them")
        if plan.notes:
            plan.steps.append(f"move {plan.notes} notes onto the contact")
        if plan.runs:
            plan.steps.append(f"carry {plan.runs} import runs into contact_import_runs")
        present = [t for t in SUPERSEDED if _has_table(conn, t)]
        if present:
            plan.steps.append(f"drop {', '.join(present)} once every row is copied")
        return plan
    finally:
        conn.close()


def migrate(path: Path) -> MigrationPlan:
    """Apply the migration. Raises :class:`MigrationError` rather than half-do it."""
    plan = inspect(path)
    if not plan.needed:
        return plan

    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    # Manual transaction control. Under the default isolation_level the
    # driver only opens a transaction before DML, so CREATE / ALTER / DROP
    # run in autocommit and rollback() cannot undo them -- a failed verify
    # would leave the book rebuilt but not populated. With None, the
    # explicit BEGIN below covers the schema changes too.
    conn.isolation_level = None
    # A migration is the worst moment to fail on a transient lock: it would
    # leave the book half-converted. Wait for the writer instead.
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN")
        _rebuild_contacts(conn)
        # Now that `contacts` is core-shaped, the rest of the shared schema
        # can be built: its indexes cover columns the old table lacked, so
        # this could not have run first.  One definition, from core.
        from navig_contacts.store import create_schema

        create_schema(conn)
        _absorb_social_profiles(conn)
        _absorb_notes(conn)
        _absorb_runs(conn)
        _routes_from_identifiers(conn)
        _absorb_liveness(conn)
        _verify(conn, plan)
        for table in SUPERSEDED:
            if _has_table(conn, table):
                conn.execute(f"DROP TABLE {table}")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version (version) VALUES (2)")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.close()

    # Opening through the store creates whatever indexes are still missing.
    from navig_contacts.store import ContactStore

    ContactStore(db_path=path)
    return plan


def _rebuild_contacts(conn: sqlite3.Connection) -> None:
    """
    Recreate `contacts` with core's exact declaration and copy every row.

    A column rename cannot give `alias` its NOCASE collation, and a book without
    it accepts `Alice` and `alice` as two people that `resolve_alias` then
    cannot tell apart.
    """
    old = _columns(conn, "contacts")

    # Since 3.25 sqlite rewrites every *other* table's foreign key to follow a
    # renamed table -- so this rename would silently repoint
    # contact_identifiers/aliases/sources/routes at `_premerge_contacts`, and
    # dropping it below would leave four tables referencing a table that does
    # not exist. The book then looks fine until the first write with foreign
    # keys on, which fails with `no such table: main._premerge_contacts`.
    # legacy_alter_table is sqlite's own answer for a rebuild of this shape:
    # rename without touching anything that points at it.
    conn.execute("PRAGMA legacy_alter_table = ON")
    try:
        conn.execute("ALTER TABLE contacts RENAME TO _premerge_contacts")
    finally:
        conn.execute("PRAGMA legacy_alter_table = OFF")
    conn.execute("""
        CREATE TABLE contacts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            alias           TEXT NOT NULL UNIQUE COLLATE NOCASE,
            display_name    TEXT NOT NULL DEFAULT '',
            default_network TEXT,
            fallbacks_json  TEXT DEFAULT '[]',
            created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            tier            TEXT,
            primary_phone   TEXT,
            primary_email   TEXT,
            birthday        TEXT,
            org             TEXT,
            job_title       TEXT,
            city            TEXT,
            country         TEXT,
            photo_path      TEXT,
            source_profile  TEXT,
            is_org          INTEGER DEFAULT 0,
            is_deleted      INTEGER DEFAULT 0,
            merged_into     INTEGER,
            notes           TEXT
        )
    """)

    def carried(name: str, fallback: str = "NULL") -> str:
        return name if name in old else fallback

    conn.execute(f"""
        INSERT INTO contacts (id, alias, display_name, created_at, updated_at,
                              tier, primary_phone, primary_email, birthday, org,
                              job_title, city, country, photo_path,
                              source_profile, is_org, is_deleted, merged_into)
        SELECT id,
               uid,
               COALESCE(full_name, ''),
               COALESCE({carried('created_at')}, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
               COALESCE({carried('updated_at')}, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
               {carried('tier')}, {carried('primary_phone')}, {carried('primary_email')},
               {carried('birthday')}, {carried('org')}, {carried('job_title')},
               {carried('city')}, {carried('country')}, {carried('photo_path')},
               {carried('source_profile')},
               COALESCE({carried('is_org')}, 0), COALESCE({carried('is_deleted')}, 0),
               {carried('merged_into')}
          FROM _premerge_contacts
    """)
    conn.execute("DROP TABLE _premerge_contacts")


def _absorb_social_profiles(conn: sqlite3.Connection) -> None:
    """A handle is an identifier, not a table of its own."""
    if not _has_table(conn, "social_profiles"):
        return
    conn.execute("""
        INSERT OR IGNORE INTO contact_identifiers
            (contact_id, kind, value_norm, value_raw, is_primary, source_file, created_at)
        SELECT s.contact_id, LOWER(s.platform), LOWER(s.handle), s.handle, 0,
               'social_profiles', datetime('now')
          FROM social_profiles s
          JOIN contacts c ON c.id = s.contact_id
         WHERE s.handle IS NOT NULL AND TRIM(s.handle) <> ''
    """)


def _absorb_notes(conn: sqlite3.Connection) -> None:
    """The pre-merge book kept notes in a side table; they belong on the contact."""
    if not _has_table(conn, "contact_status"):
        return
    conn.execute("""
        UPDATE contacts SET notes = (
            SELECT s.notes FROM contact_status s
             WHERE s.contact_id = contacts.id
               AND s.notes IS NOT NULL AND TRIM(s.notes) <> ''
             LIMIT 1)
         WHERE notes IS NULL
    """)


def _absorb_runs(conn: sqlite3.Connection) -> None:
    """
    Carry the import history across.

    A rename is not enough: an interrupted open of this book can already have
    created an empty ``contact_import_runs``, and then the rename would fail —
    or, worse, be skipped and the history left stranded in the old table.
    """
    if not _has_table(conn, "import_runs"):
        return
    columns = [c for c in _columns(conn, "import_runs")
               if c in _columns(conn, "contact_import_runs")]
    if not columns:
        return
    names = ", ".join(columns)
    conn.execute(f"INSERT OR IGNORE INTO contact_import_runs ({names}) "
                 f"SELECT {names} FROM import_runs")


def _routes_from_identifiers(conn: sqlite3.Connection) -> None:
    """Identifiers that name a transport become routes dispatch can send on."""
    if not _has_table(conn, "contact_identifiers"):
        return
    conn.execute("""
        INSERT OR IGNORE INTO contact_routes (contact_id, network, address, priority)
        SELECT i.contact_id,
               CASE i.kind WHEN 'phone' THEN 'sms' ELSE i.kind END,
               i.value_norm, 0
          FROM contact_identifiers i
          JOIN contacts c ON c.id = i.contact_id
         WHERE i.kind IN ('phone', 'email', 'telegram')
    """)


def _absorb_liveness(conn: sqlite3.Connection) -> None:
    """
    Carry the handle-liveness verdicts onto the routes they are about.

    The pre-merge book recorded, per handle, whether it still resolves --
    `gone`, `invalid`, `not_a_user` -- and each verdict cost a lookup to
    produce. Dropping `social_profiles` without them would throw that away
    silently, and the next export would re-emit every dead handle as though
    nothing had ever been checked.

    A route is a transport, so "this transport no longer resolves" is a fact
    about the route rather than about the person: it lands in `meta_json`,
    merged rather than overwritten so nothing already there is lost.
    """
    if not _has_table(conn, "social_profiles"):
        return
    if "resolve_state" not in _columns(conn, "social_profiles"):
        return
    conn.execute("""
        UPDATE contact_routes SET meta_json = json_patch(
            COALESCE(NULLIF(meta_json, ''), '{}'),
            (SELECT json_object('resolve_state', s.resolve_state,
                                'resolve_checked_at', s.resolve_checked_at)
               FROM social_profiles s
              WHERE s.contact_id = contact_routes.contact_id
                AND LOWER(s.handle) = contact_routes.address
                AND LOWER(s.platform) = contact_routes.network
                AND s.resolve_state IS NOT NULL
              LIMIT 1))
         WHERE EXISTS (
            SELECT 1 FROM social_profiles s
             WHERE s.contact_id = contact_routes.contact_id
               AND LOWER(s.handle) = contact_routes.address
               AND LOWER(s.platform) = contact_routes.network
               AND s.resolve_state IS NOT NULL)
    """)


def _verify(conn: sqlite3.Connection, plan: MigrationPlan) -> None:
    """Refuse to drop anything until every row has demonstrably arrived."""
    contacts = _count(conn, "contacts")
    if contacts != plan.contacts:
        raise MigrationError(
            f"contacts went from {plan.contacts} to {contacts} — aborting with "
            f"the book unchanged")
    if plan.runs:
        carried = _count(conn, "contact_import_runs")
        if carried < plan.runs:
            raise MigrationError(
                f"only {carried} of {plan.runs} import runs carried across")
    if plan.routes_to_create:
        routes = _count(conn, "contact_routes")
        if routes < plan.routes_to_create:
            raise MigrationError(
                f"only {routes} of {plan.routes_to_create} routes created")
