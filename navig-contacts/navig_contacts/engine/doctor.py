"""
Checks that a contact book still says the same thing in both of its halves.

Every data bug this book has had was the same shape: two things that should
describe each other drifting apart unnoticed. A route with no identifier meant
an import filed the person twice. An identifier the unique index does not cover
meant two people could own one address. A contact merged away but still holding
a live route meant `dispatch send` could reach a record nothing points at.

None of those raise. They sit there being wrong, so something has to go and
look.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from navig_sdk.host import command_name

# The command a user types here: `navig contacts` inside navig, `navig-contacts` on its own.
CMD = command_name("contacts")


@dataclass
class Finding:
    check: str
    detail: str
    count: int
    fix: str = ""
    rows: list = field(default_factory=list)


def diagnose(conn) -> list[Finding]:
    """Every disagreement found in this book, worst first."""
    findings: list[Finding] = []
    for check in (
        _routes_without_identifiers,
        _identifiers_without_routes,
        _addresses_owned_twice,
        _live_routes_on_a_merged_contact,
        _orphaned_rows,
        _tier_disagrees_with_the_evidence,
        _premerge_leftovers,
        _dangling_foreign_keys,
    ):
        findings.extend(check(conn))
    findings.sort(key=lambda f: -f.count)
    return findings


def _rows(conn, sql, params=()) -> list:
    try:
        return conn.execute(sql, params).fetchall()
    except Exception:
        return []


def _routes_without_identifiers(conn) -> list[Finding]:
    """
    A route whose address is not recorded as an identifier.

    This is what let an address-book import file someone twice: the merge key
    reads identifiers, so a contact carrying only a route was invisible to it.
    """
    from navig_contacts.store import ROUTE_NETWORK_IDENTIFIER

    rows = _rows(conn, """
        SELECT c.alias, r.network, r.address
          FROM contact_routes r
          JOIN contacts c ON c.id = r.contact_id
         WHERE NOT EXISTS (
                   SELECT 1 FROM contact_identifiers i
                    WHERE i.contact_id = r.contact_id
                      AND i.value_norm = LOWER(r.address))
         ORDER BY c.alias
    """)
    rows = [r for r in rows if (r[1] or "").lower() in ROUTE_NETWORK_IDENTIFIER]
    if not rows:
        return []
    return [Finding(
        check="routes with no identifier",
        detail="an import cannot recognise these contacts and would file them "
               "again under a different alias",
        count=len(rows),
        fix=f"{CMD} doctor --fix",
        rows=[f"@{r[0]}  {r[1]}:{r[2]}" for r in rows[:10]],
    )]


def _identifiers_without_routes(conn) -> list[Finding]:
    """An address navig could send to, that dispatch cannot see."""
    from navig_contacts.store import IDENTIFIER_ROUTE_NETWORK

    findings = []
    for kind, network in IDENTIFIER_ROUTE_NETWORK.items():
        rows = _rows(conn, """
            SELECT c.alias, i.value_norm
              FROM contact_identifiers i
              JOIN contacts c ON c.id = i.contact_id
             WHERE i.kind = ?
               AND COALESCE(c.is_deleted, 0) = 0 AND c.merged_into IS NULL
               AND NOT EXISTS (
                       SELECT 1 FROM contact_routes r
                        WHERE r.contact_id = i.contact_id
                          AND r.address = i.value_norm)
             ORDER BY c.alias
        """, (kind,))
        if rows:
            findings.append(Finding(
                check=f"{kind} identifiers with no {network} route",
                detail="you know how to reach these people, but "
                       "`navig dispatch send` does not",
                count=len(rows),
                fix=f"{CMD} doctor --fix",
                rows=[f"@{r[0]}  {r[1]}" for r in rows[:10]],
            ))
    return findings


def _addresses_owned_twice(conn) -> list[Finding]:
    """
    One address on two contacts.

    Legitimate for a household landline; a duplicate person otherwise. The
    unique index prevents it for identifiers, so anything here got in through
    routes, which are deliberately per-contact.
    """
    rows = _rows(conn, """
        SELECT r.address, COUNT(DISTINCT r.contact_id) n,
               GROUP_CONCAT(DISTINCT c.alias)
          FROM contact_routes r
          JOIN contacts c ON c.id = r.contact_id
         WHERE COALESCE(c.is_deleted, 0) = 0 AND c.merged_into IS NULL
         GROUP BY r.address HAVING n > 1
         ORDER BY n DESC
    """)
    if not rows:
        return []
    return [Finding(
        check="one address, several contacts",
        detail="a shared line is fine; two records of the same person is not — "
               f"`{CMD} merge` if they are one",
        count=len(rows),
        rows=[f"{r[0]}  ->  {r[2]}" for r in rows[:10]],
    )]


def _live_routes_on_a_merged_contact(conn) -> list[Finding]:
    """A record nothing points at, that a message could still be sent to."""
    rows = _rows(conn, """
        SELECT c.alias, COUNT(r.id)
          FROM contacts c JOIN contact_routes r ON r.contact_id = c.id
         WHERE c.merged_into IS NOT NULL OR c.is_deleted = 1
         GROUP BY c.alias
    """)
    if not rows:
        return []
    return [Finding(
        check="routes on a merged or deleted contact",
        detail="these should have moved to the surviving contact; a message "
               "sent here goes to a record nothing points at",
        count=sum(r[1] for r in rows),
        fix=f"{CMD} doctor --fix",
        rows=[f"@{r[0]}  {r[1]} route(s)" for r in rows[:10]],
    )]


def _orphaned_rows(conn) -> list[Finding]:
    """Evidence attached to a contact that no longer exists."""
    findings = []
    for table in ("contact_identifiers", "contact_aliases", "contact_sources",
                  "contact_routes"):
        rows = _rows(conn, f"""
            SELECT COUNT(*) FROM {table} t
             WHERE NOT EXISTS (SELECT 1 FROM contacts c WHERE c.id = t.contact_id)
        """)
        if rows and rows[0][0]:
            findings.append(Finding(
                check=f"orphaned {table}",
                detail="rows pointing at a contact that is gone",
                count=rows[0][0],
                fix=f"{CMD} doctor --fix",
            ))
    return findings


def _tier_disagrees_with_the_evidence(conn) -> list[Finding]:
    """
    A contact filed as unreachable that has a phone, or the reverse.

    `tier` is the whole model — "a contact is legit only if it has a phone
    number" — so a tier that does not match the identifiers is the model lying.
    """
    rows = _rows(conn, """
        SELECT c.alias, c.tier,
               (SELECT COUNT(*) FROM contact_identifiers i
                 WHERE i.contact_id = c.id AND i.kind = 'phone') AS phones
          FROM contacts c
         WHERE COALESCE(c.is_deleted, 0) = 0 AND c.merged_into IS NULL
           AND ((c.tier = 'verified' AND phones = 0)
             OR (c.tier IN ('email', 'handle', 'archive') AND phones > 0))
    """)
    if not rows:
        return []
    return [Finding(
        check="tier disagrees with the identifiers",
        detail="'verified' means a phone number validated; these say otherwise",
        count=len(rows),
        fix=f"{CMD} doctor --fix",
        rows=[f"@{r[0]}  tier={r[1]}  phones={r[2]}" for r in rows[:10]],
    )]


def _premerge_leftovers(conn) -> list[Finding]:
    """
    Tables from before the address book and the routing table were one.

    Empty ones are usually not leftovers at all: the retired space-local tool
    re-creates them from its own DDL on *every* invocation, so a single
    `python cli.py list` against a migrated book puts them back. That is why
    that tool has to be retired rather than merely left broken.
    """
    present = [r[0] for r in _rows(conn, """
        SELECT name FROM sqlite_master WHERE type = 'table'
           AND name IN ('social_profiles', 'contact_status', 'import_runs')
    """)]
    if not present:
        return []
    counts = {t: (_rows(conn, f"SELECT COUNT(*) FROM {t}") or [[0]])[0][0]
              for t in present}
    with_rows = {t: n for t, n in counts.items() if n}
    if with_rows:
        return [Finding(
            check="tables from before the merge, still holding rows",
            detail="this book has not been migrated, or the migration did not "
                   "finish; the same fact lives in two places",
            count=sum(with_rows.values()),
            fix=f"{CMD} migrate --apply",
            rows=[f"{t}  {n} row(s)" for t, n in with_rows.items()],
        )]
    return [Finding(
        check="empty tables from before the merge",
        detail="something re-created these after the migration — almost "
               "certainly the retired space-local tool, which rebuilds its own "
               "schema every time it runs",
        count=len(present),
        fix=f"{CMD} doctor --fix",
        rows=present,
    )]


# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------

def _dangling_foreign_keys(conn) -> list[Finding]:
    """
    A table whose foreign key names a table that is not there.

    This one is not a drift, it is a scar. Since 3.25 sqlite rewrites every
    other table's foreign key to follow a renamed table, so a rebuild that
    renames `contacts` out of the way repoints its four child tables at the
    temporary name, and dropping it leaves them pointing at nothing.

    Nothing complains while foreign keys are off, which is sqlite's default --
    so the book reads perfectly and every write fails the moment something
    turns them on. `navig contacts` does.
    """
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    broken = []
    for name in sorted(tables):
        for row in _rows(conn, f'PRAGMA foreign_key_list("{name}")'):
            target = row[2]
            if target not in tables:
                broken.append((name, target))
    if not broken:
        return []
    return [Finding(
        check="foreign keys pointing at a table that is gone",
        detail="every write to these fails once foreign keys are on, which is "
               f"how `{CMD}` opens the book — so it reads fine and "
               "cannot be changed",
        count=len(broken),
        fix=f"{CMD} doctor --fix",
        rows=[f"{name}  ->  {target}" for name, target in broken],
    )]


def repair(conn) -> dict[str, int]:
    """
    Fix what can be fixed without a judgement call.

    Deliberately narrow: it writes the missing halves of things that already
    agree, and moves nothing between people. Deciding whether two contacts on
    one number are one person is a merge, and that is a human's call.
    """
    from navig_contacts.store import (
        IDENTIFIER_ROUTE_NETWORK, ROUTE_NETWORK_IDENTIFIER, normalize_phone,
    )

    done = {"foreign_keys_repointed": 0,
            "identifiers_added": 0, "routes_added": 0, "routes_moved": 0,
            "orphans_removed": 0, "tiers_corrected": 0,
            "empty_premerge_tables_dropped": 0}

    # First, because every repair below writes to these tables and a dangling
    # foreign key makes all of it fail.
    done["foreign_keys_repointed"] = _repoint_foreign_keys(conn)

    # Only ever the empty ones. A pre-merge table with rows in it is data
    # that has not been carried across yet, and dropping it would be the
    # one unrecoverable thing this command could do.
    for table in ("social_profiles", "contact_status", "import_runs"):
        rows = _rows(conn, f"SELECT COUNT(*) FROM {table}")
        if rows and rows[0][0] == 0:
            conn.execute(f"DROP TABLE {table}")
            done["empty_premerge_tables_dropped"] += 1

    for contact_id, network, address in conn.execute(
        "SELECT r.contact_id, r.network, r.address FROM contact_routes r "
        "JOIN contacts c ON c.id = r.contact_id"
    ).fetchall():
        kind = ROUTE_NETWORK_IDENTIFIER.get((network or "").lower())
        if not kind:
            continue
        value = normalize_phone(address) if kind == "phone" else address.strip()
        if not value:
            continue
        done["identifiers_added"] += conn.execute(
            "INSERT OR IGNORE INTO contact_identifiers "
            "(contact_id, kind, value_norm, value_raw, is_primary, source_file) "
            "VALUES (?, ?, ?, ?, 0, 'doctor')",
            (contact_id, kind, value.lower(), address)).rowcount

    for kind, network in IDENTIFIER_ROUTE_NETWORK.items():
        done["routes_added"] += conn.execute("""
            INSERT OR IGNORE INTO contact_routes (contact_id, network, address, priority)
            SELECT i.contact_id, ?, i.value_norm, 0
              FROM contact_identifiers i
              JOIN contacts c ON c.id = i.contact_id
             WHERE i.kind = ? AND COALESCE(c.is_deleted, 0) = 0
               AND c.merged_into IS NULL
        """, (network, kind)).rowcount

    done["routes_moved"] = conn.execute("""
        UPDATE OR IGNORE contact_routes SET contact_id = (
            SELECT c.merged_into FROM contacts c WHERE c.id = contact_id)
         WHERE contact_id IN (SELECT id FROM contacts WHERE merged_into IS NOT NULL)
    """).rowcount

    for table in ("contact_identifiers", "contact_aliases", "contact_sources",
                  "contact_routes"):
        done["orphans_removed"] += conn.execute(
            f"DELETE FROM {table} WHERE NOT EXISTS "
            f"(SELECT 1 FROM contacts c WHERE c.id = {table}.contact_id)").rowcount

    done["tiers_corrected"] = conn.execute("""
        UPDATE contacts SET tier = CASE
                WHEN (SELECT COUNT(*) FROM contact_identifiers i
                       WHERE i.contact_id = contacts.id AND i.kind = 'phone') > 0
                    THEN 'verified'
                WHEN (SELECT COUNT(*) FROM contact_identifiers i
                       WHERE i.contact_id = contacts.id AND i.kind = 'email') > 0
                    THEN 'email'
                ELSE 'handle' END
         WHERE COALESCE(is_deleted, 0) = 0 AND merged_into IS NULL
           AND tier IS NOT NULL
           AND tier <> CASE
                WHEN (SELECT COUNT(*) FROM contact_identifiers i
                       WHERE i.contact_id = contacts.id AND i.kind = 'phone') > 0
                    THEN 'verified'
                WHEN (SELECT COUNT(*) FROM contact_identifiers i
                       WHERE i.contact_id = contacts.id AND i.kind = 'email') > 0
                    THEN 'email'
                ELSE 'handle' END
    """).rowcount
    return done


def _repoint_foreign_keys(conn) -> int:
    """
    Give a table its foreign key back by rebuilding it from core's declaration.

    Rebuild rather than edit `sqlite_master` through `writable_schema`: the
    rows are copied out, the broken table dropped, core's `create_schema`
    recreates it with the right reference *and* its indexes -- including the
    partial unique index that is the merge key -- and the rows go back.

    This is DDL, so it is *not* covered by the caller's transaction: python's
    sqlite3 commits around a CREATE or DROP. What protects the rows instead is
    the order -- `_fix_<name>` holds a full copy before the drop and is only
    dropped once they are back -- and the backup `doctor --fix` takes before it
    starts. A failure part-way leaves the copy on disk, named after the table
    it came from.
    """
    from navig_contacts.store import create_schema

    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    broken = [
        name for name in sorted(tables)
        if any(row[2] not in tables
               for row in _rows(conn, f'PRAGMA foreign_key_list("{name}")'))
    ]
    if not broken:
        return 0

    # The copy below has no foreign keys of its own; enforcement has to be off
    # while the real table does not exist, or the copy-back trips on itself.
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        for name in broken:
            columns = [r[1] for r in conn.execute(f'PRAGMA table_info("{name}")')]
            cols = ", ".join(f'"{c}"' for c in columns)
            conn.execute(f'CREATE TABLE "_fix_{name}" AS SELECT * FROM "{name}"')
            conn.execute(f'DROP TABLE "{name}"')
            create_schema(conn)
            conn.execute(
                f'INSERT INTO "{name}" ({cols}) SELECT {cols} FROM "_fix_{name}"')
            conn.execute(f'DROP TABLE "_fix_{name}"')
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    return len(broken)
