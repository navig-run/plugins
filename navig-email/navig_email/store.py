"""The mailroom's one transactional store: ``<navig data dir>/mailroom.db``.

Everything that must survive a restart and must never be decided twice lives here — not in
the JSON/JSONL files the rest of the plugin writes (those stay human-facing ledgers and
reports). In particular this is the **only** record of an outgoing message: there is no
second queue anywhere (not in the edge Worker, not in the cron store) that could race it.

Tables:

* ``outbox`` — one row per outgoing message (:mod:`navig_email.outbox`). ``job_id`` is
  immutable; ``op_key`` is UNIQUE, so the same intent enqueued twice yields one job;
  ``version`` increments on every write, and every state change is a conditional
  ``UPDATE … WHERE state=? AND version=?`` — two writers can never both win.
* ``outbox_events`` — append-only transition history: who (user / os / telegram / policy /
  scheduler) moved which job from what to what, and why.
* ``meta`` — small key/values: the global send pause, per-account disable flags.
* ``audit`` — every mailroom action with a redacted summary (no bodies, no secrets).
* ``policies`` / ``extractions`` / ``digest_*`` / ``lifecycle`` / ``cb_tokens`` — owned by the
  auto-reply, digest, cleanup and Telegram modules; created here so one schema version
  describes the whole file.

Writes go straight through ``BEGIN IMMEDIATE`` transactions, never a write batcher: a state
transition that is merely *queued* is not a state transition.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from navig_sdk.store import BaseStore
from navig_sdk.store import utcnow as _utcnow

DB_FILENAME = "mailroom.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    job_id           TEXT PRIMARY KEY,
    op_key           TEXT NOT NULL UNIQUE,
    origin           TEXT NOT NULL DEFAULT 'user',
    policy_id        TEXT,
    provider         TEXT NOT NULL DEFAULT 'gmail',
    space_root       TEXT NOT NULL DEFAULT '',
    account          TEXT NOT NULL DEFAULT '',
    from_addr        TEXT NOT NULL DEFAULT '',
    from_name        TEXT NOT NULL DEFAULT '',
    identity         TEXT NOT NULL DEFAULT '',
    to_addrs         TEXT NOT NULL DEFAULT '',
    cc_addrs         TEXT NOT NULL DEFAULT '',
    subject          TEXT NOT NULL DEFAULT '',
    body             TEXT NOT NULL DEFAULT '',
    thread_id        TEXT NOT NULL DEFAULT '',
    reply_to_msg     TEXT NOT NULL DEFAULT '',
    in_reply_to      TEXT NOT NULL DEFAULT '',
    refs             TEXT NOT NULL DEFAULT '',
    message_id       TEXT NOT NULL DEFAULT '',
    content_hash     TEXT NOT NULL DEFAULT '',
    approved_hash    TEXT NOT NULL DEFAULT '',
    approved_by      TEXT NOT NULL DEFAULT '',
    approved_at      TEXT,
    state            TEXT NOT NULL,
    not_before       TEXT,
    claimed_by       TEXT NOT NULL DEFAULT '',
    claimed_at       TEXT,
    sending_at       TEXT,
    sent_at          TEXT,
    provider_msg_id  TEXT NOT NULL DEFAULT '',
    attempts         INTEGER NOT NULL DEFAULT 0,
    reconcile_tries  INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT NOT NULL DEFAULT '',
    quality_json     TEXT NOT NULL DEFAULT '',
    style            TEXT NOT NULL DEFAULT '',
    model            TEXT NOT NULL DEFAULT '',
    version          INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outbox_due ON outbox (state, not_before);
CREATE INDEX IF NOT EXISTS idx_outbox_thread ON outbox (account, thread_id);

CREATE TABLE IF NOT EXISTS outbox_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL,
    at          TEXT NOT NULL,
    actor       TEXT NOT NULL DEFAULT '',
    from_state  TEXT NOT NULL DEFAULT '',
    to_state    TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_outbox_events_job ON outbox_events (job_id, id);

CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS audit (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    at       TEXT NOT NULL,
    actor    TEXT NOT NULL DEFAULT '',
    action   TEXT NOT NULL,
    account  TEXT NOT NULL DEFAULT '',
    ref      TEXT NOT NULL DEFAULT '',
    summary  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit (at);

CREATE TABLE IF NOT EXISTS policies (
    policy_id   TEXT PRIMARY KEY,
    template    TEXT NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 0,
    spec_json   TEXT NOT NULL DEFAULT '{}',
    enabled_by  TEXT NOT NULL DEFAULT '',
    enabled_at  TEXT,
    version     INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS extractions (
    provider_id        TEXT NOT NULL,
    content_hash       TEXT NOT NULL,
    extractor_version  TEXT NOT NULL,
    account            TEXT NOT NULL DEFAULT '',
    result_json        TEXT NOT NULL DEFAULT '{}',
    created_at         TEXT NOT NULL,
    PRIMARY KEY (provider_id, content_hash, extractor_version)
);

CREATE TABLE IF NOT EXISTS digest_items (
    item_id       TEXT PRIMARY KEY,
    fingerprint   TEXT NOT NULL,
    kind          TEXT NOT NULL,
    title         TEXT NOT NULL DEFAULT '',
    data_json     TEXT NOT NULL DEFAULT '{}',
    score         REAL NOT NULL DEFAULT 0,
    first_seen    TEXT NOT NULL,
    run_id        TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'new'
);
CREATE INDEX IF NOT EXISTS idx_digest_items_fp ON digest_items (fingerprint);

CREATE TABLE IF NOT EXISTS digest_sources (
    item_id     TEXT NOT NULL,
    account     TEXT NOT NULL DEFAULT '',
    message_id  TEXT NOT NULL,
    PRIMARY KEY (item_id, account, message_id)
);
CREATE INDEX IF NOT EXISTS idx_digest_sources_msg ON digest_sources (account, message_id);

CREATE TABLE IF NOT EXISTS digest_runs (
    run_id          TEXT PRIMARY KEY,
    category        TEXT NOT NULL DEFAULT 'all',
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    item_count      INTEGER NOT NULL DEFAULT 0,
    delivery_state  TEXT NOT NULL DEFAULT 'pending',
    delivery_ref    TEXT NOT NULL DEFAULT '',
    note            TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS lifecycle (
    account      TEXT NOT NULL,
    message_id   TEXT NOT NULL,
    state        TEXT NOT NULL DEFAULT 'seen',
    run_id       TEXT NOT NULL DEFAULT '',
    trash_after  TEXT,
    updated_at   TEXT NOT NULL,
    note         TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (account, message_id)
);

CREATE TABLE IF NOT EXISTS cb_tokens (
    token       TEXT PRIMARY KEY,
    action      TEXT NOT NULL,
    args_json   TEXT NOT NULL DEFAULT '{}',
    chat_id     TEXT NOT NULL DEFAULT '',
    single_use  INTEGER NOT NULL DEFAULT 0,
    expires_at  TEXT NOT NULL,
    used_at     TEXT,
    result      TEXT NOT NULL DEFAULT ''
);
"""


def default_db_path() -> Path:
    """``$NAVIG_MAILROOM_DB`` when set, else ``<navig data dir>/mailroom.db``."""
    override = os.environ.get("NAVIG_MAILROOM_DB", "").strip()
    if override:
        return Path(override)
    from navig_sdk.host import data_dir  # noqa: PLC0415 - resolved per call, never at import

    return data_dir() / DB_FILENAME


class MailroomStore(BaseStore):
    SCHEMA_VERSION = 1

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(_SCHEMA)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One ``BEGIN IMMEDIATE`` … ``COMMIT``; rolls back on error."""
        with self._lock:
            conn = self._get_conn()
            old = conn.isolation_level
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.isolation_level = old

    # ── meta ────────────────────────────────────────────────────────────────

    def meta_get(self, key: str, default: str = "") -> str:
        row = self._read_one("SELECT value FROM meta WHERE key = ?", (key,))
        return str(row[0]) if row else default

    def meta_set(self, key: str, value: str) -> None:
        self._write(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # ── audit ───────────────────────────────────────────────────────────────

    def audit(
        self,
        action: str,
        *,
        actor: str = "",
        account: str = "",
        ref: str = "",
        summary: str = "",
        conn: sqlite3.Connection | None = None,
    ) -> None:
        """Record one action. *summary* must already be redacted — no bodies, no secrets."""
        sql = "INSERT INTO audit (at, actor, action, account, ref, summary) VALUES (?, ?, ?, ?, ?, ?)"
        params = (_utcnow(), actor, action, account, ref, summary[:500])
        if conn is not None:
            conn.execute(sql, params)
        else:
            self._write(sql, params)

    def audit_rows(self, limit: int = 100, *, account: str = "") -> list[dict[str, Any]]:
        if account:
            rows = self._read_all(
                "SELECT * FROM audit WHERE account = ? ORDER BY id DESC LIMIT ?", (account, limit)
            )
        else:
            rows = self._read_all("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


_STORE: MailroomStore | None = None
_STORE_PATH: Path | None = None


def get_store() -> MailroomStore:
    """The process-wide store for the current ``default_db_path()`` (re-opened if it moved —
    tests point ``NAVIG_MAILROOM_DB`` at a tmp file)."""
    global _STORE, _STORE_PATH
    path = default_db_path()
    if _STORE is None or _STORE_PATH != path:
        _STORE = MailroomStore(path)
        _STORE_PATH = path
    return _STORE
