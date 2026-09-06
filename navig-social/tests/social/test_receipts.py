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
