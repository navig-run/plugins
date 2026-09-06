"""Engagement ledger — the **measure** half of create → publish → measure.

Where :mod:`navig_social.social.receipts` records *what we published* (keyed by
``utm_campaign``), this records *how it performed*: per-campaign metrics
(clicks / views / likes / replies …) that arrive from an ingest — a UTM
click beacon, a webhook, a platform poll, or a manual entry — and join back to
the receipt ledger on the campaign slug, closing the loop for the next brief.

Backed by :class:`BaseStore`, owned by navig-social (no core schema change).
Values are additive counts, so re-ingesting the same source just accumulates —
callers that report *totals* should send deltas.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

from navig.store.base import BaseStore, _utcnow

logger = logging.getLogger(__name__)

# Recognised metric names (free-form is allowed, but these get first-class columns
# in the joined report and validate a CLI typo like "click" vs "clicks").
KNOWN_METRICS = ("clicks", "views", "likes", "replies", "shares", "reach")


class EngagementStore(BaseStore):
    """SQLite ledger of per-campaign engagement events (additive counts)."""

    SCHEMA_VERSION = 1
    PRAGMAS = {"cache_size": -2000}

    def __init__(self, db_path: Path | None = None):
        super().__init__(db_path or _default_db_path())

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS engagement (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign    TEXT NOT NULL DEFAULT '',
                network     TEXT,
                post_id     TEXT,
                metric      TEXT NOT NULL,
                value       INTEGER NOT NULL DEFAULT 0,
                source      TEXT NOT NULL DEFAULT 'manual',
                created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE INDEX IF NOT EXISTS idx_engagement_campaign
                ON engagement (campaign, metric);
        """)

    def _migrate(self, conn: sqlite3.Connection, from_version: int, to_version: int) -> None:
        pass  # v1 is initial

    def record(
        self,
        *,
        campaign: str,
        metric: str,
        value: int = 1,
        network: str | None = None,
        post_id: str | None = None,
        source: str = "manual",
    ) -> int:
        """Persist one engagement **event** (additive). For absolute platform
        snapshots (views/likes from `navig social sync`), use :meth:`upsert_metric`."""
        cur = self._write(
            "INSERT INTO engagement (campaign, network, post_id, metric, value, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (campaign or "", network, post_id, metric, int(value), source or "manual", _utcnow()),
        )
        return int(cur.lastrowid or 0)

    def upsert_metric(
        self,
        *,
        campaign: str,
        network: str,
        post_id: str,
        metric: str,
        value: int,
        source: str = "platform",
    ) -> None:
        """Replace-latest for an **absolute** metric (e.g. a synced view count).

        Deletes any prior row with the same (campaign, network, post_id, metric,
        source) then inserts the current value — so re-running `navig social sync`
        keeps the latest count instead of summing it (the rollups use SUM, which is
        correct for additive click *events* but would double-count absolute state)."""
        self._write(
            "DELETE FROM engagement WHERE campaign = ? AND network = ? AND post_id = ? "
            "AND metric = ? AND source = ?",
            (campaign or "", network, post_id, metric, source),
        )
        self._write(
            "INSERT INTO engagement (campaign, network, post_id, metric, value, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (campaign or "", network, post_id, metric, int(value), source, _utcnow()),
        )

    def campaign_metrics(self, campaign: str) -> dict[str, int]:
        """Totals per metric for one campaign, e.g. ``{"clicks": 42, "views": 120}``."""
        rows = self._read_all(
            "SELECT metric, SUM(value) AS total FROM engagement WHERE campaign = ? GROUP BY metric",
            (campaign,),
        )
        return {r["metric"]: int(r["total"] or 0) for r in rows}

    def network_metrics(self, campaign: str) -> dict[str, dict[str, int]]:
        """Per-network metric totals for one campaign, e.g.
        ``{"twitter": {"clicks": 30}, "telegram": {"clicks": 12}}`` — the
        breakdown behind the campaign scorecard."""
        rows = self._read_all(
            "SELECT network, metric, SUM(value) AS total FROM engagement "
            "WHERE campaign = ? GROUP BY network, metric",
            (campaign,),
        )
        out: dict[str, dict[str, int]] = {}
        for r in rows:
            out.setdefault(r["network"] or "", {})[r["metric"]] = int(r["total"] or 0)
        return out

    def rollup(self, *, limit: int = 100) -> dict[str, dict[str, int]]:
        """All campaigns → their metric totals, e.g.
        ``{"launch": {"clicks": 42, "views": 120}}`` (most-recent campaigns first)."""
        rows = self._read_all(
            "SELECT campaign, metric, SUM(value) AS total, MAX(created_at) AS last_at "
            "FROM engagement GROUP BY campaign, metric ORDER BY last_at DESC LIMIT ?",
            (limit * len(KNOWN_METRICS) + limit,),  # generous cap; grouped rows expand per metric
        )
        out: dict[str, dict[str, int]] = {}
        for r in rows:
            out.setdefault(r["campaign"] or "", {})[r["metric"]] = int(r["total"] or 0)
        return out


_store: EngagementStore | None = None


def get_engagement() -> EngagementStore:
    """Process-wide singleton (respects ``NAVIG_DATA_DIR`` via ``paths.data_dir()``)."""
    global _store
    if _store is None:
        _store = EngagementStore()
    return _store


def _default_db_path() -> Path:
    from navig.platform import paths

    return paths.data_dir() / "social_engagement.db"


def record_engagement_from_event(event: dict[str, Any]) -> int | None:
    """Record engagement from a navig-signals-shaped ingest event.

    The counterpart to :func:`navig_social.social.receipts.receipt_to_signals_event`:
    when the signals click/engagement wire lands, each event's ``meta`` (campaign +
    metric + value) feeds straight into this ledger. Returns the row id, or ``None``
    when the event lacks a campaign/metric (ignored, not an error).
    """
    meta = (event or {}).get("meta") or {}
    campaign = (meta.get("campaign") or "").strip()
    metric = (meta.get("metric") or "").strip()
    if not campaign or not metric:
        return None
    try:
        value = int(meta.get("value", 1))
    except (TypeError, ValueError):
        value = 1
    try:
        return get_engagement().record(
            campaign=campaign, metric=metric, value=value,
            network=meta.get("network"), post_id=meta.get("post_id"),
            source=event.get("source") or "signals",
        )
    except Exception as exc:  # noqa: BLE001 - ingest must never crash the caller
        logger.warning("engagement ingest skipped: %s", exc)
        return None
