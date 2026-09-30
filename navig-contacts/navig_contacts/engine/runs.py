"""
Import-run bookkeeping.

Every import records the `max(contacts.id)` watermark it started from, so the
exact set of contacts an import added stays answerable afterwards:

    SELECT * FROM contacts WHERE id > max_contact_id_before

That is what `export --since-last-import` uses, and it is why a new-contacts
report no longer requires the caller to note max(id) by hand before importing.

Watermarks are used rather than timestamps because `contacts.id` is a
monotonic AUTOINCREMENT: two imports inside the same second stay
distinguishable, and a soft-deleted contact keeps its id.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional

from .db import get_db


def _now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def max_contact_id(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(MAX(id), 0) FROM contacts").fetchone()[0]


def new_session_id() -> str:
    """An id shared by every file imported in one CLI invocation."""
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S%f")


def start_run(source: str, input_path: str, kind: str = "json",
              session_id: Optional[str] = None) -> tuple[int, int]:
    """
    Open an import_runs row.  Returns (run_id, max_contact_id_before).
    """
    with get_db() as conn:
        before = max_contact_id(conn)
        cur = conn.execute(
            """INSERT INTO contact_import_runs
               (session_id, source, input_path, kind, started_at, max_contact_id_before)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (session_id, source, input_path, kind, _now(), before),
        )
        return cur.lastrowid, before


def finish_run(run_id: int, summary) -> None:
    """Close an import_runs row with the resulting ImportSummary."""
    with get_db() as conn:
        conn.execute(
            """UPDATE contact_import_runs
                  SET finished_at = ?, max_contact_id_after = ?,
                      total = ?, inserted = ?, duplicates = ?, errors = ?,
                      photos_matched = ?, photos_unmatched = ?
                WHERE id = ?""",
            (
                _now(), max_contact_id(conn),
                summary.total, summary.inserted, summary.duplicates,
                summary.errors, summary.photos_matched, summary.photos_unmatched,
                run_id,
            ),
        )


def latest_run(conn: Optional[sqlite3.Connection] = None) -> Optional[sqlite3.Row]:
    """Most recent *completed* import run, or None if there has never been one."""
    sql = ("SELECT * FROM contact_import_runs WHERE finished_at IS NOT NULL "
           "ORDER BY id DESC LIMIT 1")
    if conn is not None:
        return conn.execute(sql).fetchone()
    with get_db() as c:
        return c.execute(sql).fetchone()


def latest_session() -> Optional[dict]:
    """
    The most recent import *invocation*, collapsed to one pseudo-run.

    This is what "since the last import" has to mean: importing six files in
    one command produces six runs, and the last of them (often a no-op dedup
    pass) would otherwise report zero new contacts for the whole session.
    The watermark is the lowest of the session's, so the report spans every
    file the invocation touched.
    """
    last = latest_run()
    if last is None:
        return None

    with get_db() as conn:
        if last["session_id"]:
            rows = conn.execute(
                "SELECT * FROM contact_import_runs WHERE session_id = ? ORDER BY id",
                (last["session_id"],),
            ).fetchall()
        else:
            rows = [last]  # pre-session_id row — treat it as its own session

    return {
        "id": f"{rows[0]['id']}–{rows[-1]['id']}" if len(rows) > 1 else str(rows[0]["id"]),
        "session_id": last["session_id"],
        "source": ", ".join(dict.fromkeys(r["source"] or "—" for r in rows)),
        "finished_at": rows[-1]["finished_at"],
        "max_contact_id_before": min(r["max_contact_id_before"] for r in rows),
        "inserted": sum(r["inserted"] or 0 for r in rows),
        "runs": len(rows),
    }


def get_run(run_id: int) -> Optional[sqlite3.Row]:
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM contact_import_runs WHERE id = ?", (run_id,)
        ).fetchone()


def list_runs(limit: int = 20) -> list[sqlite3.Row]:
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM contact_import_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


def session_watermark(run_ids: list[int]) -> Optional[int]:
    """
    Lowest `max_contact_id_before` across several runs — the watermark for a
    whole multi-file import invocation, so one report covers all of its files.
    """
    if not run_ids:
        return None
    with get_db() as conn:
        placeholders = ",".join("?" * len(run_ids))
        row = conn.execute(
            f"SELECT MIN(max_contact_id_before) FROM contact_import_runs "
            f"WHERE id IN ({placeholders})",
            run_ids,
        ).fetchone()
    return row[0] if row else None
