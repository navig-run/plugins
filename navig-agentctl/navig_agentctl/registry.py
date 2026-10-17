"""AgentCtlStore — the persistent session + task-ownership registry.

Phase 1 (Observer) makes two things durable that today only exist ephemerally:

* **sessions** — every agent session seen (from ``navig procs`` snapshots and,
  later, heartbeats), with its project, worktree, owner and last state. The
  Control Center reads this to show *who owns what* across restarts.
* **bindings** — which session is working on which task record (board card or
  work item). Phase 2 writes these; Phase 1 reads them.

Nothing here executes work: the store is a ledger, not a queue. Task truth stays
in ``board.db`` / ``work.db``; this table only answers "which live session owns
this task", which neither of those stores can.

Isolation: ``AgentCtlStore(db_path=...)`` — tests MUST pass an explicit path.
``store_dir()`` isolation via ``NAVIG_STORE_DIR`` is honoured only when
``db_path`` is omitted (same rule as every other store).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from navig.store.base import BaseStore, utcnow
from navig_agentctl.runs import RunsMixin, migrate_runs

#: Sessions not re-observed within this window are marked "stale" (not deleted —
#: a stale row is the recovery breadcrumb Phase 3 needs).
STALE_AFTER_HOURS = 24.0

#: State-change events kept per session; older ones pruned on write.
KEEP_EVENTS = 20


class AgentCtlStore(RunsMixin, BaseStore):
    SCHEMA_VERSION = 2
    PRAGMAS = {"cache_size": -500}

    def __init__(self, db_path: Path | None = None):
        if db_path is None:
            db_path = _default_db_path()
        super().__init__(db_path)

    # ── schema ─────────────────────────────────────────────────────────────

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS session (
                session_id      TEXT PRIMARY KEY,     -- claude session id, or pid:<pid> fallback
                pid             INTEGER,
                project         TEXT NOT NULL DEFAULT '',
                project_root    TEXT NOT NULL DEFAULT '',
                worktree        TEXT NOT NULL DEFAULT '',
                agent_kind      TEXT NOT NULL DEFAULT '',   -- claude-code | codex | …
                editor          TEXT NOT NULL DEFAULT '',   -- vscode | cursor | '' (CLI)
                owner           TEXT NOT NULL DEFAULT '',   -- e.g. "code[1234]"
                state           TEXT NOT NULL DEFAULT 'unknown',  -- active|idle|young|unknown|stale
                reason          TEXT NOT NULL DEFAULT '',
                first_seen_at   TEXT NOT NULL,
                last_seen_at    TEXT NOT NULL,
                last_activity_at TEXT
            );
            CREATE TABLE IF NOT EXISTS session_event (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  TEXT NOT NULL,
                at          TEXT NOT NULL,
                state       TEXT NOT NULL,
                detail      TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS ix_session_state ON session(state);
            CREATE INDEX IF NOT EXISTS ix_session_project ON session(project);
            CREATE INDEX IF NOT EXISTS ix_event_session ON session_event(session_id, id);

            CREATE TABLE IF NOT EXISTS binding (
                task_ref    TEXT PRIMARY KEY,   -- "board:<id>" | "work:<slug>"
                session_id  TEXT NOT NULL,
                bound_at    TEXT NOT NULL,
                released_at TEXT,
                note        TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS ix_binding_session ON binding(session_id);
        """)
        migrate_runs(conn)

    def _migrate(self, conn: sqlite3.Connection, from_version: int, to_version: int) -> None:
        if from_version < 2:
            migrate_runs(conn)  # v2: Agent Monitor run ledger (additive)

    # ── sessions ───────────────────────────────────────────────────────────

    def upsert_session(self, row: dict[str, Any]) -> bool:
        """Record (or refresh) one observed session. Idempotent per session_id.

        Returns False (and writes nothing) when the row carries no session_id —
        callers count real recordings, never attempted ones.
        """
        now = utcnow()
        sid = str(row.get("session_id") or "").strip()
        if not sid:
            return False
        state = str(row.get("state") or "unknown")

        existing = self._read_one(
            "SELECT state FROM session WHERE session_id = ?", (sid,)
        )
        self._write(
            """
            INSERT INTO session (session_id, pid, project, project_root, worktree,
                                 agent_kind, editor, owner, state, reason,
                                 first_seen_at, last_seen_at, last_activity_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                pid = excluded.pid,
                project = excluded.project,
                project_root = excluded.project_root,
                worktree = excluded.worktree,
                agent_kind = excluded.agent_kind,
                editor = excluded.editor,
                owner = excluded.owner,
                state = excluded.state,
                reason = excluded.reason,
                last_seen_at = excluded.last_seen_at,
                last_activity_at = excluded.last_activity_at
            """,
            (
                sid,
                int(row["pid"]) if row.get("pid") else None,
                str(row.get("project") or ""),
                str(row.get("project_root") or ""),
                str(row.get("worktree") or ""),
                str(row.get("agent_kind") or ""),
                str(row.get("editor") or ""),
                str(row.get("owner") or ""),
                state,
                str(row.get("reason") or ""),
                now,
                now,
                str(row.get("last_activity_at") or "") or None,
            ),
        )
        # Event ledger: append on state change only (a re-snapshot of the
        # same state is not history).
        if (existing is None) or (existing["state"] != state):
            self._write(
                "INSERT INTO session_event (session_id, at, state, detail) VALUES (?, ?, ?, ?)",
                (sid, now, state, str(row.get("reason") or "")[:300]),
            )
            self._write(
                "DELETE FROM session_event WHERE session_id = ? AND id NOT IN ("
                "SELECT id FROM session_event WHERE session_id = ? "
                "ORDER BY id DESC LIMIT ?)",
                (sid, sid, KEEP_EVENTS),
            )
        return True

    def annotate_session(self, row: dict[str, Any]) -> bool:
        """Record an EXTENSION heartbeat: annotation-only upsert.

        Only the editor-known context is written (project_root, worktree,
        agent_kind, editor, owner, last_activity_at). Daemon-derived fields
        — pid, state, reason, last_seen_at — are NEVER touched: the process
        table owns truth about liveness, the heartbeat can only add what the
        daemon cannot see (which workspace, which editor, which roster owner).

        A non-empty incoming annotation never erases a known one (empty
        string = "I don't know", not "forget"). A row the daemon has never
        seen is created with state='unknown' until the next snapshot
        classifies it; last_seen_at starts at now and is NOT refreshed by
        later heartbeats, so a dead session still ages into 'stale'.

        Returns False when session_id is missing/empty.
        """
        now = utcnow()
        sid = str(row.get("session_id") or "").strip()
        if not sid:
            return False
        existing = self._read_one(
            "SELECT session_id FROM session WHERE session_id = ?", (sid,)
        )
        self._write(
            """
            INSERT INTO session (session_id, pid, project, project_root, worktree,
                                 agent_kind, editor, owner, state, reason,
                                 first_seen_at, last_seen_at, last_activity_at)
            VALUES (?, NULL, '', ?, ?, ?, ?, ?, 'unknown', '', ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                project_root = CASE WHEN excluded.project_root != ''
                    THEN excluded.project_root ELSE session.project_root END,
                worktree = CASE WHEN excluded.worktree != ''
                    THEN excluded.worktree ELSE session.worktree END,
                agent_kind = CASE WHEN excluded.agent_kind != ''
                    THEN excluded.agent_kind ELSE session.agent_kind END,
                editor = CASE WHEN excluded.editor != ''
                    THEN excluded.editor ELSE session.editor END,
                owner = CASE WHEN excluded.owner != ''
                    THEN excluded.owner ELSE session.owner END,
                last_activity_at = COALESCE(excluded.last_activity_at, session.last_activity_at)
            """,
            (
                sid,
                str(row.get("project_root") or ""),
                str(row.get("worktree") or ""),
                str(row.get("agent_kind") or ""),
                str(row.get("editor") or ""),
                str(row.get("owner") or ""),
                now,
                now,
                str(row.get("last_activity_at") or "") or None,
            ),
        )
        if existing is None:
            # A heartbeat-created row enters history honestly: unknown state,
            # attributed to the annotation source.
            self._write(
                "INSERT INTO session_event (session_id, at, state, detail) "
                "VALUES (?, ?, 'unknown', 'heartbeat annotation')",
                (sid, now),
            )
        return True

    def list_sessions(self, *, include_stale: bool = True) -> list[dict[str, Any]]:
        rows = self._read_all("SELECT * FROM session ORDER BY last_seen_at DESC")
        out = [dict(r) for r in rows]
        if not include_stale:
            out = [r for r in out if r["state"] != "stale"]
        return out

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        row = self._read_one("SELECT * FROM session WHERE session_id = ?", (session_id,))
        return dict(row) if row else None

    def mark_stale(self) -> int:
        """Sessions not re-seen within STALE_AFTER_HOURS become state='stale'."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=STALE_AFTER_HOURS)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        cur = self._write(
            "UPDATE session SET state = 'stale' WHERE state != 'stale' AND last_seen_at < ?",
            (cutoff,),
        )
        return cur.rowcount or 0

    def session_events(self, session_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self._read_all(
                "SELECT at, state, detail FROM session_event "
                "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            )
        ]

    # ── task bindings ──────────────────────────────────────────────────────

    def bind(self, task_ref: str, session_id: str, *, note: str = "") -> None:
        self._write(
            """
            INSERT INTO binding (task_ref, session_id, bound_at, note)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(task_ref) DO UPDATE SET
                session_id = excluded.session_id,
                bound_at = excluded.bound_at,
                released_at = NULL,
                note = excluded.note
            """,
            (task_ref, session_id, utcnow(), note[:300]),
        )

    def release(self, task_ref: str) -> None:
        self._write(
            "UPDATE binding SET released_at = ? WHERE task_ref = ? AND released_at IS NULL",
            (utcnow(), task_ref),
        )

    def bindings(self, *, active_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM binding"
        if active_only:
            sql += " WHERE released_at IS NULL"
        return [dict(r) for r in self._read_all(sql + " ORDER BY bound_at DESC")]

    def binding_for(self, task_ref: str) -> dict[str, Any] | None:
        row = self._read_one(
            "SELECT * FROM binding WHERE task_ref = ? AND released_at IS NULL", (task_ref,)
        )
        return dict(row) if row else None


def _default_db_path() -> Path:
    from navig.platform.paths import store_dir

    return store_dir() / "agentctl.db"


def get_agentctl_store(db_path: Path | None = None) -> AgentCtlStore:
    return AgentCtlStore(db_path)
