"""Campaign scorecard — the receipt × engagement join + CTR + ranking.

Temp stores, monkeypatched getters — no global state. Verifies the join, the
clicks-only case, CTR (with/without views), and click-ranking.
"""

from __future__ import annotations

import pytest

from navig_social.social.engagement import EngagementStore
from navig_social.social.receipts import PublishReceiptStore
from navig_social.social.report import campaign_scorecard, leaderboard


@pytest.fixture
def stores(tmp_path, monkeypatch):
    rec = PublishReceiptStore(tmp_path / "r.db")
    eng = EngagementStore(tmp_path / "e.db")
    import navig_social.social.engagement as E
    import navig_social.social.receipts as R

    monkeypatch.setattr(R, "get_publish_receipts", lambda: rec)
    monkeypatch.setattr(E, "get_engagement", lambda: eng)
    return rec, eng


def test_network_metrics_breakdown(tmp_path):
    e = EngagementStore(tmp_path / "e.db")
    e.record(campaign="launch", network="twitter", metric="clicks", value=30)
    e.record(campaign="launch", network="telegram", metric="clicks", value=12)
    e.record(campaign="launch", network="twitter", metric="views", value=1000)
    nm = e.network_metrics("launch")
    assert nm["twitter"] == {"clicks": 30, "views": 1000}
    assert nm["telegram"] == {"clicks": 12}


def test_campaign_scorecard_joins_ranks_and_ctr(stores):
    rec, eng = stores
    rec.record(campaign="launch", network="twitter", url="https://a?utm_campaign=launch", ok=True)
    rec.record(campaign="launch", network="telegram", ok=False, error="no token")
    eng.record(campaign="launch", network="twitter", metric="clicks", value=30)
    eng.record(campaign="launch", network="twitter", metric="views", value=1000)
    eng.record(campaign="launch", network="telegram", metric="clicks", value=5)

    card = campaign_scorecard("launch")
    nets = {n["network"]: n for n in card["networks"]}
    assert card["networks"][0]["network"] == "twitter"  # ranked by clicks
    assert nets["twitter"]["clicks"] == 30 and nets["twitter"]["ok"] == 1
    assert abs(nets["twitter"]["ctr"] - 0.03) < 1e-9  # 30 / 1000
    assert nets["telegram"]["ok"] == 0 and nets["telegram"]["clicks"] == 5
    assert nets["telegram"]["ctr"] is None  # no views → no CTR
    assert card["totals"]["clicks"] == 35 and card["totals"]["posts"] == 2


def test_scorecard_includes_clicks_only_networks(stores):
    _, eng = stores
    eng.record(campaign="launch", network="mastodon", metric="clicks", value=7)  # no publish recorded
    card = campaign_scorecard("launch")
    m = {n["network"]: n for n in card["networks"]}
    assert m["mastodon"]["clicks"] == 7 and m["mastodon"]["posts"] == 0


def test_scorecard_empty_for_unknown_campaign(stores):
    card = campaign_scorecard("nope")
    assert card["networks"] == [] and card["totals"]["clicks"] == 0


def test_leaderboard_ranks_by_clicks(stores):
    rec, eng = stores
    rec.record(campaign="a", network="twitter", url="u", ok=True)
    rec.record(campaign="b", network="twitter", url="u", ok=True)
    eng.record(campaign="a", metric="clicks", value=5)
    eng.record(campaign="b", metric="clicks", value=50)
    board = leaderboard()
    assert [c["campaign"] for c in board] == ["b", "a"]  # b earned more clicks
    assert board[0]["clicks"] == 50
