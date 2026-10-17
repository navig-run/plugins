"""`navig social sync` — pull live platform metrics into the engagement ledger.

Fake publishers (no HTTP), temp stores. Verifies the payoff: synced view counts
upsert into engagement so the campaign scorecard's CTR goes *real*; re-syncing is
idempotent (absolute counts replace, never sum); and networks with no read API or
no token are skipped, not failed. Only successful posts that carry a platform
post_id are syncable.
"""

from __future__ import annotations

from navig_social.social.base import BasePublisher
from navig_social.social.engagement import EngagementStore
from navig_social.social.receipts import PublishReceiptStore


class _ReadablePub(BasePublisher):
    """A publisher that exposes metrics (overrides fetch_metrics)."""

    def __init__(self, name: str, metrics: dict[str, int], configured: bool = True):
        self.name = name
        self._metrics = metrics
        self._configured = configured

    def is_configured(self) -> bool:
        return self._configured

    async def fetch_metrics(self, post_id: str) -> dict[str, int] | None:
        return self._metrics


class _NoReadPub(BasePublisher):
    """A publisher with no public-metrics endpoint (inherits the base None)."""

    def __init__(self, name: str):
        self.name = name

    def is_configured(self) -> bool:
        return True


class _FakeRegistry:
    def __init__(self, pubs):
        self._pubs = {p.name: p for p in pubs}

    def get(self, name):
        return self._pubs.get(name)


def _wire(tmp_path, monkeypatch, pubs):
    """Point the command's three getters at temp stores + a fake registry."""
    import navig_social.social.engagement as E
    import navig_social.social.receipts as R
    import navig_social.social.registry as REG

    rec = PublishReceiptStore(tmp_path / "r.db")
    eng = EngagementStore(tmp_path / "e.db")
    monkeypatch.setattr(R, "get_publish_receipts", lambda: rec)
    monkeypatch.setattr(E, "get_engagement", lambda: eng)
    monkeypatch.setattr(REG, "get_publisher_registry", lambda: _FakeRegistry(pubs))
    return rec, eng


def test_sync_upserts_metrics_and_ctr_goes_real(tmp_path, monkeypatch):
    from navig_social.commands.social import social_sync
    from navig_social.social.report import campaign_scorecard

    pubs = [_ReadablePub("twitter", {"views": 1000, "likes": 12, "reposts": 3, "replies": 4})]
    rec, eng = _wire(tmp_path, monkeypatch, pubs)
    rec.record(campaign="launch", network="twitter", post_id="123",
               url="https://a?utm_campaign=launch", ok=True)
    eng.record(campaign="launch", network="twitter", metric="clicks", value=30)  # from the redirect ledger

    social_sync(campaign="launch", limit=500, as_json=False)

    m = eng.network_metrics("launch")["twitter"]
    assert m["views"] == 1000 and m["clicks"] == 30
    card = campaign_scorecard("launch")
    tw = next(n for n in card["networks"] if n["network"] == "twitter")
    assert abs(tw["ctr"] - 0.03) < 1e-9  # 30 clicks / 1000 views — a real CTR, no manual entry


def test_resync_replaces_not_sums(tmp_path, monkeypatch):
    from navig_social.commands.social import social_sync

    rec, eng = _wire(tmp_path, monkeypatch, [_ReadablePub("twitter", {"views": 500})])
    rec.record(campaign="c", network="twitter", post_id="1", ok=True)

    social_sync(campaign="c", limit=500, as_json=False)
    social_sync(campaign="c", limit=500, as_json=False)  # run twice — must not double

    assert eng.network_metrics("c")["twitter"]["views"] == 500  # replaced, not 1000


def test_unsupported_and_unconnected_networks_are_skipped(tmp_path, monkeypatch):
    from navig_social.commands.social import social_sync

    pubs = [
        _NoReadPub("telegram"),                                    # no read API
        _ReadablePub("twitter", {"views": 9}, configured=False),  # not connected
    ]
    rec, eng = _wire(tmp_path, monkeypatch, pubs)
    rec.record(campaign="c", network="telegram", post_id="t1", ok=True)
    rec.record(campaign="c", network="twitter", post_id="x1", ok=True)

    social_sync(campaign="c", limit=500, as_json=False)

    assert eng.network_metrics("c") == {}  # neither wrote a platform metric — skipped, not failed


def test_only_ok_posts_with_ids_are_synced(tmp_path, monkeypatch):
    from navig_social.commands.social import social_sync

    rec, eng = _wire(tmp_path, monkeypatch, [_ReadablePub("twitter", {"views": 100})])
    rec.record(campaign="c", network="twitter", post_id="ok1", ok=True)    # syncable
    rec.record(campaign="c", network="twitter", post_id=None, ok=True)     # no id → skip
    rec.record(campaign="c", network="twitter", post_id="bad", ok=False)   # failed → skip

    social_sync(campaign="c", limit=500, as_json=False)

    # network_metrics SUMs per network across posts; each fake post returns 100, so a
    # total of 100 proves exactly one post (ok1) synced — the others were correctly skipped.
    assert eng.network_metrics("c")["twitter"]["views"] == 100


def test_duplicate_receipts_collapse_to_one_post(tmp_path, monkeypatch):
    from navig_social.commands.social import social_sync

    rec, eng = _wire(tmp_path, monkeypatch, [_ReadablePub("twitter", {"views": 100})])
    # same post re-published (two receipts, same post_id) must sync once
    rec.record(campaign="c", network="twitter", post_id="1", ok=True)
    rec.record(campaign="c", network="twitter", post_id="1", ok=True)

    social_sync(campaign="c", limit=500, as_json=False)

    assert eng.network_metrics("c")["twitter"]["views"] == 100  # not 200


def test_json_path_still_upserts(tmp_path, monkeypatch):
    from navig_social.commands.social import social_sync

    rec, eng = _wire(tmp_path, monkeypatch, [_ReadablePub("twitter", {"views": 42})])
    rec.record(campaign="c", network="twitter", post_id="1", ok=True)

    social_sync(campaign="c", limit=500, as_json=True)  # JSON branch runs after the upserts

    assert eng.network_metrics("c")["twitter"]["views"] == 42


def test_nothing_to_sync_is_graceful(tmp_path, monkeypatch):
    from navig_social.commands.social import social_sync

    _rec, eng = _wire(tmp_path, monkeypatch, [_ReadablePub("twitter", {"views": 1})])

    social_sync(campaign="empty", limit=500, as_json=False)  # no receipts at all

    assert eng.network_metrics("empty") == {}
