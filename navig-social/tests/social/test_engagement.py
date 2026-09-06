"""Engagement ledger — record / rollup / campaign totals, the signals adapter,
and the HTTP ingest handler. Temp SQLite paths only; nothing global touched.
"""

from __future__ import annotations

from navig_social.social.engagement import (
    EngagementStore,
    record_engagement_from_event,
)


def _store(tmp_path) -> EngagementStore:
    return EngagementStore(tmp_path / "engagement.db")


def test_record_and_campaign_metrics_are_additive(tmp_path):
    s = _store(tmp_path)
    s.record(campaign="launch", metric="clicks", value=10)
    s.record(campaign="launch", metric="clicks", value=5)  # accumulates
    s.record(campaign="launch", metric="views", value=100)
    assert s.campaign_metrics("launch") == {"clicks": 15, "views": 100}


def test_upsert_metric_replaces_not_accumulates(tmp_path):
    s = _store(tmp_path)
    # absolute platform snapshot for one post — re-syncing must replace, not sum
    s.upsert_metric(campaign="c", network="twitter", post_id="1", metric="views", value=100)
    s.upsert_metric(campaign="c", network="twitter", post_id="1", metric="views", value=250)
    assert s.campaign_metrics("c") == {"views": 250}  # latest wins, not 350


def test_upsert_metric_is_scoped_per_post_and_source(tmp_path):
    s = _store(tmp_path)
    # different posts SUM across the network; a manual event on a different source coexists
    s.upsert_metric(campaign="c", network="twitter", post_id="1", metric="views", value=100)
    s.upsert_metric(campaign="c", network="twitter", post_id="2", metric="views", value=50)
    s.record(campaign="c", network="twitter", metric="views", value=7, source="manual")
    assert s.network_metrics("c")["twitter"]["views"] == 157  # 100 + 50 + 7


def test_rollup_groups_by_campaign(tmp_path):
    s = _store(tmp_path)
    s.record(campaign="a", metric="clicks", value=3)
    s.record(campaign="b", metric="views", value=9)
    roll = s.rollup()
    assert roll["a"] == {"clicks": 3}
    assert roll["b"] == {"views": 9}


def test_record_from_signals_event(tmp_path, monkeypatch):
    import navig_social.social.engagement as E

    s = EngagementStore(tmp_path / "e.db")
    monkeypatch.setattr(E, "get_engagement", lambda: s)
    ev = {"source": "signals",
          "meta": {"campaign": "launch", "network": "twitter", "metric": "clicks", "value": 7}}
    rid = record_engagement_from_event(ev)
    assert rid and s.campaign_metrics("launch") == {"clicks": 7}


def test_record_from_event_ignores_incomplete(tmp_path, monkeypatch):
    import navig_social.social.engagement as E

    s = EngagementStore(tmp_path / "e.db")
    monkeypatch.setattr(E, "get_engagement", lambda: s)
    assert record_engagement_from_event({"meta": {"campaign": "x"}}) is None  # no metric
    assert record_engagement_from_event({}) is None  # no meta


# ── HTTP ingest handler (lightweight fake request; no aiohttp server) ────────


class _FakeReq:
    def __init__(self, body=None, query=None):
        self._body = body or {}
        self.query = query or {}

    async def json(self):
        return self._body


async def test_ingest_route_records(tmp_path, monkeypatch):
    import navig_social.social.engagement as E
    from navig_social.deck_routes.engagement import handle_engagement_ingest

    s = EngagementStore(tmp_path / "e.db")
    monkeypatch.setattr(E, "get_engagement", lambda: s)
    resp = await handle_engagement_ingest(
        _FakeReq({"campaign": "launch", "metric": "clicks", "value": 12})
    )
    assert resp.status == 200
    assert s.campaign_metrics("launch") == {"clicks": 12}


async def test_ingest_route_rejects_missing_fields(tmp_path):
    from navig_social.deck_routes.engagement import handle_engagement_ingest

    resp = await handle_engagement_ingest(_FakeReq({"campaign": "x"}))  # no metric
    assert resp.status == 400
