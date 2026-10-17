"""Publish-receipt ledger — record / list / campaign summary + the signals seam.

Uses a temp SQLite path (never the real ~/.navig store), so nothing global is
touched. Verifies recording is best-effort (a store failure never propagates).
"""

from __future__ import annotations

from navig_social.social.receipts import (
    PublishReceiptStore,
    receipt_to_signals_event,
    record_receipts,
)


def _store(tmp_path) -> PublishReceiptStore:
    return PublishReceiptStore(tmp_path / "receipts.db")


def test_record_and_list_roundtrip(tmp_path):
    s = _store(tmp_path)
    s.record(campaign="launch", network="twitter", post_id="1",
             url="https://x?utm_campaign=launch", ok=True)
    s.record(campaign="launch", network="telegram", ok=False, error="no token")

    rows = s.list(campaign="launch")
    assert len(rows) == 2
    tw = next(r for r in rows if r["network"] == "twitter")
    assert tw["ok"] is True and tw["post_id"] == "1" and "utm_campaign=launch" in tw["url"]
    tg = next(r for r in rows if r["network"] == "telegram")
    assert tg["ok"] is False and tg["error"] == "no token"


def test_campaigns_summary(tmp_path):
    s = _store(tmp_path)
    s.record(campaign="a", network="twitter", ok=True)
    s.record(campaign="a", network="telegram", ok=True)
    s.record(campaign="b", network="twitter", ok=False, error="x")

    camps = {c["campaign"]: c for c in s.campaigns()}
    assert camps["a"]["count"] == 2 and camps["a"]["ok"] == 2
    assert camps["b"]["count"] == 1 and camps["b"]["ok"] == 0


def test_record_receipts_batch(tmp_path, monkeypatch):
    import navig_social.social.receipts as R

    s = PublishReceiptStore(tmp_path / "r.db")
    monkeypatch.setattr(R, "get_publish_receipts", lambda: s)
    n = R.record_receipts([
        {"campaign": "c", "network": "twitter", "post_id": "1", "url": "u", "ok": True, "error": None},
        {"campaign": "c", "network": "telegram", "ok": False, "error": "e"},
    ])
    assert n == 2 and len(s.list(campaign="c")) == 2


def test_record_receipts_is_soft_on_failure(monkeypatch):
    # A storage failure must never propagate into the publish path.
    import navig_social.social.receipts as R

    class _Boom:
        def record(self, **_kw):
            raise RuntimeError("db down")

    monkeypatch.setattr(R, "get_publish_receipts", lambda: _Boom())
    assert record_receipts([{"campaign": "c", "network": "x", "ok": True}]) == 0  # logged, not raised


def test_receipt_to_signals_event_shape():
    ev = receipt_to_signals_event(
        {"ok": True, "network": "twitter", "campaign": "launch",
         "post_id": "1", "url": "https://x?utm_campaign=launch"}
    )
    assert ev["source"] == "social-publish"
    assert "twitter" in ev["title"] and ev["meta"]["campaign"] == "launch"
    assert ev["meta"]["url"].endswith("launch")

    fail = receipt_to_signals_event({"ok": False, "network": "telegram", "error": "no token"})
    assert "failed" in fail["title"].lower() and fail["body"] == "no token"


# ── v1 ledger opens again (the silent-brick regression) ─────────────────────

def test_v1_ledger_migrates_and_records(tmp_path):
    """A real v1 publish_receipts.db must OPEN, migrate to v2 and record.

    The bug this pins: ``_create_schema`` ran ``CREATE INDEX (content_id)``
    inside the script BEFORE the version check — on a v1 table that raises
    ``no such column``, store init dies, and every recording soft-fails
    silently (a live install's ledger was found still v1 and unwritable).
    """
    import sqlite3

    db = tmp_path / "v1.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
        CREATE TABLE publish_receipts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign    TEXT NOT NULL DEFAULT '',
            network     TEXT NOT NULL,
            target      TEXT NOT NULL DEFAULT '',
            post_id     TEXT,
            url         TEXT,
            ok          INTEGER NOT NULL DEFAULT 0,
            error       TEXT,
            created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        INSERT INTO schema_version (version) VALUES (1);
        INSERT INTO publish_receipts (campaign, network, ok) VALUES ('legacy', 'twitter', 1);
    """)
    conn.commit()
    conn.close()

    s = PublishReceiptStore(db)
    assert s.get_schema_version() == PublishReceiptStore.SCHEMA_VERSION
    rows = s.list(campaign="legacy")
    assert len(rows) == 1 and rows[0]["source"] == "fanout" and rows[0]["content_id"] is None
    # identity write works on the migrated table, and a second open is idempotent
    s.record(campaign="legacy", network="telegram", content_id="schema:wave1:x", ok=True)
    assert len(s.list(content_id="schema:wave1:x")) == 1
    s.close()
    again = PublishReceiptStore(db)
    assert len(again.list(campaign="legacy")) == 2
