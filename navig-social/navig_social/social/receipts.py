"""Publish-receipt ledger — a durable, campaign-tagged record of every live fan-out.

This is the "receipt" half of the create → publish → **measure** loop: fan-out
returns ephemeral ``PublishReceipt`` objects; here we persist them so there's a
lasting answer to "what did we publish, where, when, and did it land?". Each row
carries the **UTM'd URL**, which is the join key a future navig-signals
click/engagement ingest correlates on (``utm_campaign`` == the recorded campaign
slug), closing the loop back to the next brief.

Backed by :class:`BaseStore` (WAL + schema versioning + serialized writes) and
owned by navig-social — no core schema change. Recording is best-effort: a
storage hiccup must never break the publish path.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

from navig_sdk.store import BaseStore
from navig_sdk.store import utcnow as _utcnow

logger = logging.getLogger(__name__)


class PublishReceiptStore(BaseStore):
    """SQLite ledger of publish receipts (append-only in practice)."""

    SCHEMA_VERSION = 2
    PRAGMAS = {"cache_size": -2000}

    def __init__(self, db_path: Path | None = None):
        super().__init__(db_path or _default_db_path())

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        """Fresh v2 schema; harmless on an existing v1 ledger.

        ⚠ ``BaseStore._init_schema`` calls this BEFORE it reads the stored
        version, so on a real v1 ledger ``CREATE TABLE IF NOT EXISTS`` is a
        no-op and the v2 columns do not exist yet — an unguarded
        ``CREATE INDEX (content_id)`` inside the script raises ``no such
        column`` HERE, the store never initialises, and every recording
        soft-fails silently until someone notices a dead ledger (observed on a
        live install with a v1 ``publish_receipts.db``). The two v2 indexes
        are therefore created individually, guarded; ``_migrate`` creates them
        right after this returns.
        """
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS publish_receipts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign    TEXT NOT NULL DEFAULT '',
                network     TEXT NOT NULL,
                target      TEXT NOT NULL DEFAULT '',
                post_id     TEXT,
                url         TEXT,
                ok          INTEGER NOT NULL DEFAULT 0,
                error       TEXT,
                content_id     TEXT,
                content_version INTEGER,
                source         TEXT NOT NULL DEFAULT 'fanout',
                idempotency_key TEXT,
                created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE INDEX IF NOT EXISTS idx_receipts_campaign
                ON publish_receipts (campaign, created_at);
        """)
        for stmt in (
            "CREATE INDEX IF NOT EXISTS idx_receipts_content "
            "ON publish_receipts (content_id, created_at)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_receipts_idem "
            "ON publish_receipts (idempotency_key) "
            "WHERE idempotency_key IS NOT NULL AND idempotency_key != ''",
        ):
            try:
                conn.execute(stmt)  # noqa: S608 — literal DDL
            except sqlite3.OperationalError as exc:
                if "no such column" not in str(exc).lower():
                    raise
                # v1 table: _migrate adds the columns, then these run there.

    def _migrate(self, conn: sqlite3.Connection, from_version: int, to_version: int) -> None:
        if from_version < 2:
            # v2: content identity + source + idempotency (additive; source
            # defaults so every existing row stays a fan-out receipt). Each
            # statement is guarded: a migration interrupted before the version
            # stamp re-runs on the next open and must not die on
            # ``duplicate column name``.
            for ddl in (
                "ALTER TABLE publish_receipts ADD COLUMN content_id TEXT",
                "ALTER TABLE publish_receipts ADD COLUMN content_version INTEGER",
                "ALTER TABLE publish_receipts ADD COLUMN source TEXT "
                "NOT NULL DEFAULT 'fanout'",
                "ALTER TABLE publish_receipts ADD COLUMN idempotency_key TEXT",
                "CREATE INDEX IF NOT EXISTS idx_receipts_content "
                "ON publish_receipts (content_id, created_at)",
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_receipts_idem "
                "ON publish_receipts (idempotency_key) "
                "WHERE idempotency_key IS NOT NULL AND idempotency_key != ''",
            ):
                try:
                    conn.execute(ddl)
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "campaign": row["campaign"] or "",
            "network": row["network"],
            "target": row["target"] or "",
            "post_id": row["post_id"],
            "url": row["url"],
            "ok": bool(row["ok"]),
            "error": row["error"],
            "content_id": row["content_id"],
            "content_version": row["content_version"],
            "source": row["source"] or "fanout",
            "idempotency_key": row["idempotency_key"],
            "created_at": row["created_at"] or "",
        }

    def record(
        self,
        *,
        campaign: str = "",
        network: str,
        target: str = "",
        post_id: str | None = None,
        url: str | None = None,
        ok: bool = False,
        error: str | None = None,
        content_id: str | None = None,
        content_version: int | None = None,
        source: str = "fanout",
        idempotency_key: str | None = None,
    ) -> int:
        """Persist one receipt; return its row id.

        A duplicate ``idempotency_key`` violates the partial unique index —
        caught here and treated as an already-recorded row (returns 0), so a
        replayed publish can never double-count in the ledger.
        """
        try:
            cur = self._write(
                "INSERT INTO publish_receipts "
                "(campaign, network, target, post_id, url, ok, error, "
                " content_id, content_version, source, idempotency_key, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (campaign or "", network, target or "", post_id, url,
                 1 if ok else 0, error, content_id, content_version, source or "fanout",
                 idempotency_key, _utcnow()),
            )
            return int(cur.lastrowid or 0)
        except Exception as exc:  # noqa: BLE001 — duplicate key = already recorded
            if "unique" in str(exc).lower():
                logger.debug("receipt idempotency hit, not recording twice: %s", idempotency_key)
                return 0
            raise

    def list(self, *, campaign: str | None = None, content_id: str | None = None,
             limit: int = 200) -> list[dict[str, Any]]:
        """Receipts, newest first, filtered by campaign and/or content_id."""
        if campaign and content_id:
            rows = self._read_all(
                "SELECT * FROM publish_receipts WHERE campaign = ? AND content_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (campaign, content_id, limit),
            )
        elif campaign:
            rows = self._read_all(
                "SELECT * FROM publish_receipts WHERE campaign = ? ORDER BY id DESC LIMIT ?",
                (campaign, limit),
            )
        elif content_id:
            rows = self._read_all(
                "SELECT * FROM publish_receipts WHERE content_id = ? ORDER BY id DESC LIMIT ?",
                (content_id, limit),
            )
        else:
            rows = self._read_all(
                "SELECT * FROM publish_receipts ORDER BY id DESC LIMIT ?", (limit,)
            )
        return [self._row_to_dict(r) for r in rows]

    def latest_url(self, campaign: str, network: str) -> str | None:
        """The most-recent published URL for a (campaign, network) — the click
        redirect's resolution target (from OUR ledger, never a query param)."""
        row = self._read_one(
            "SELECT url FROM publish_receipts "
            "WHERE campaign = ? AND network = ? AND url IS NOT NULL AND url != '' "
            "ORDER BY id DESC LIMIT 1",
            (campaign, network),
        )
        return row["url"] if row else None

    def campaigns(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Distinct campaigns with post counts + last activity (newest first)."""
        rows = self._read_all(
            "SELECT campaign, COUNT(*) AS n, SUM(ok) AS ok_n, MAX(created_at) AS last_at "
            "FROM publish_receipts GROUP BY campaign ORDER BY last_at DESC LIMIT ?",
            (limit,),
        )
        return [
            {"campaign": r["campaign"] or "", "count": r["n"],
             "ok": r["ok_n"] or 0, "last": r["last_at"] or ""}
            for r in rows
        ]


_store: PublishReceiptStore | None = None


def get_publish_receipts() -> PublishReceiptStore:
    """Process-wide singleton (respects ``NAVIG_DATA_DIR`` via ``paths.data_dir()``)."""
    global _store
    if _store is None:
        _store = PublishReceiptStore()
    return _store


def _default_db_path() -> Path:
    from navig_sdk.host import data_dir  # navig's data dir, or the same path standalone

    return data_dir() / "publish_receipts.db"


def record_receipts(rows: list[dict[str, Any]]) -> int:
    """Persist a batch of receipt rows. Best-effort — never raises into the publish
    path (a storage failure is logged, not propagated). Returns the count recorded."""
    if not rows:
        return 0
    try:
        store = get_publish_receipts()
        for r in rows:
            store.record(**r)
        return len(rows)
    except Exception as exc:  # noqa: BLE001 - persistence must never break publishing
        logger.warning("publish-receipt recording skipped: %s", exc)
        return 0


def receipt_to_signals_event(row: dict[str, Any]) -> dict[str, Any]:
    """Shape a recorded receipt into a navig-signals ingest event payload.

    The seam a future engagement wire posts to ``/api/ingest/<source>`` so publishes
    (and later, their click-throughs keyed by ``utm_campaign``) surface in the deck's
    notify fan-out and feed the next brief. Kept pure so it's trivially testable.
    """
    ok = bool(row.get("ok"))
    network = row.get("network") or "?"
    return {
        "title": f"Published to {network}" if ok else f"Publish failed · {network}",
        "body": row.get("url") or row.get("error") or "",
        "source": "social-publish",
        "meta": {
            "campaign": row.get("campaign"),
            "network": network,
            "post_id": row.get("post_id"),
            "url": row.get("url"),
            "ok": ok,
        },
    }
