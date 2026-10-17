"""
Whether a route still reaches anyone.

A username that no longer resolves is not a person you can reach, but nothing
records that on its own -- so every export re-emits it and every run spends a
lookup rediscovering it is dead. The verdict is written down once, and it lives
on the **route** rather than on the person: a route is a transport, so "this
transport no longer resolves" is a fact about the transport. The same human may
be perfectly reachable by phone.

That distinction is the whole reason `unreachable_sql()` is not simply "has a dead
route". Dropping someone from an export because their old Telegram handle died,
while a valid mobile sits on the same contact, would be a worse bug than the
one this fixes.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

#: Verdicts meaning the route no longer reaches anybody.
DEAD_STATES = ("gone", "invalid", "not_a_user")

#: Every verdict that can be recorded. `ok` is a real answer, not the absence
#: of one -- "checked, still resolves" and "never checked" must not look alike.
STATES = ("ok",) + DEAD_STATES

#: What each verdict means, for `--help` and for the reader in six months.
MEANING = {
    "ok": "resolves to a real person",
    "gone": "does not resolve (deleted or renamed)",
    "invalid": "malformed, or rejected by the platform",
    "not_a_user": "resolves, but to a channel or a group",
}


def dead_sql(column: str = "meta_json") -> str:
    """
    SQL that is true only for a route judged dead.

    The COALESCE is the whole point, and it is not defensive noise. A route
    with no verdict has `json_extract(...) IS NULL`, `NULL IN (...)` is NULL,
    and `NOT NULL` is NULL -- so without it, three-valued logic quietly
    classifies every *unchecked* route as dead and the export empties itself.
    That is exactly the confusion this module exists to prevent, and it is easy
    to reintroduce, so it is spelled out in one place.
    """
    states = ", ".join(repr(s) for s in DEAD_STATES)
    return (f"COALESCE(json_extract({column}, '$.resolve_state') "
            f"IN ({states}), 0) = 1")


def live_sql(column: str = "meta_json") -> str:
    """True for a route that is not known to be dead -- unchecked included."""
    return f"NOT ({dead_sql(column)})"


def now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def read_verdicts(path: Path) -> list[tuple[str, str]]:
    """
    Parse a JSONL of ``{"handle": "x", "state": "gone"}`` into pairs.

    Rejects the whole file on the first bad line rather than importing half of
    it: a liveness sweep that silently skipped the lines it could not read
    would leave handles looking unchecked when they had in fact been judged.
    """
    out: list[tuple[str, str]] = []
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise ValueError(f"{path.name}:{n} is not valid JSON") from exc
        handle = str(record.get("handle") or "").strip().lstrip("@")
        state = str(record.get("state") or "gone").strip()
        if not handle:
            raise ValueError(f"{path.name}:{n} has no handle")
        if state not in STATES:
            raise ValueError(
                f"{path.name}:{n}: state {state!r} is not one of {', '.join(STATES)}")
        out.append((handle, state))
    return out


def record(conn, handle: str, state: str, network: str = "telegram",
           checked_at: str | None = None) -> int:
    """
    Write one verdict onto every matching route. Returns how many it touched.

    Merged with ``json_patch`` rather than assigned, so anything else already
    in ``meta_json`` survives -- the column is shared, and a liveness sweep has
    no business clearing a field it does not own.
    """
    if state not in STATES:
        raise ValueError(f"{state!r} is not one of {', '.join(STATES)}")
    return conn.execute(
        """
        UPDATE contact_routes
           SET meta_json = json_patch(
                   COALESCE(NULLIF(meta_json, ''), '{}'),
                   json_object('resolve_state', ?, 'resolve_checked_at', ?))
         WHERE network = ? COLLATE NOCASE
           AND address = ? COLLATE NOCASE
        """,
        (state, checked_at or now(), network, handle.lstrip("@")),
    ).rowcount


def verdict_of(meta_json: str | None) -> tuple[str | None, str | None]:
    """``(state, checked_at)`` for one route, or ``(None, None)`` if unchecked."""
    if not meta_json:
        return None, None
    try:
        meta = json.loads(meta_json)
    except ValueError:
        return None, None
    if not isinstance(meta, dict):
        return None, None
    return meta.get("resolve_state"), meta.get("resolve_checked_at")


def unreachable_sql(table: str = "contacts") -> str:
    """
    SQL for "this contact has routes, and every one of them is dead".

    Deliberately not "has a dead route". Someone whose old Telegram handle died
    but whose mobile still works is reachable, and dropping them from an export
    would lose a real contact to a cosmetic one. A contact with no routes at
    all is not unreachable either -- it is unrouted, which is a different
    problem and not this one's to judge.
    """
    return f"""
        EXISTS (SELECT 1 FROM contact_routes r
                 WHERE r.contact_id = {table}.id)
        AND NOT EXISTS (SELECT 1 FROM contact_routes r
                         WHERE r.contact_id = {table}.id
                           AND {live_sql("r.meta_json")})
    """


def counts(conn) -> dict[str, int]:
    """How many routes carry each verdict, plus how many were never checked."""
    out = {
        state: conn.execute(
            "SELECT COUNT(*) FROM contact_routes "
            "WHERE json_extract(meta_json, '$.resolve_state') = ?",
            (state,)).fetchone()[0]
        for state in STATES
    }
    out["unchecked"] = conn.execute(
        "SELECT COUNT(*) FROM contact_routes "
        "WHERE json_extract(meta_json, '$.resolve_state') IS NULL").fetchone()[0]
    return out
