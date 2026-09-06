"""Click-tracking redirects — link wrapping (opt-in) + the ledger-resolved redirect.

No live HTTP/config: the tracking base is monkeypatched and stores use temp paths.
Verifies the published link is wrapped while the *recorded* URL stays the real
destination, dev.to's canonical stays clean, and the redirect resolves from the
ledger (open-redirect-free) + records a click.
"""

from __future__ import annotations

from navig_social.social.base import BasePublisher
from navig_social.social.engagement import EngagementStore
from navig_social.social.fanout import fan_out, tracking_link
from navig_social.social.receipts import PublishReceiptStore
from navig_social.social.registry import get_publisher_registry, reset_publisher_registry
from navig_social.social.types import PublishReceipt

BRIEF = {"title": "Ship", "body": "the body", "url": "https://cybesis.com/hub", "campaign": "launch"}


class _Fake(BasePublisher):
    def __init__(self, name: str) -> None:
        self.name = name
        self.seen = None

    def is_configured(self) -> bool:
        return True

    async def publish(self, target, post):
        self.seen = post
        return PublishReceipt.success(self.name, target, id="1")


class _Req:
    def __init__(self, campaign: str, network: str) -> None:
        self.match_info = {"campaign": campaign, "network": network}


def test_tracking_link_shape():
    assert tracking_link("https://lh.example/", "launch", "twitter") == "https://lh.example/r/launch/twitter"


async def test_fan_out_wraps_links_but_records_real_target(monkeypatch):
    import navig_social.social.fanout as F
    import navig_social.social.receipts as R

    monkeypatch.setattr(F, "_tracking_base", lambda: "https://lh.example")
    captured = {}
    monkeypatch.setattr(R, "record_receipts", lambda rows: captured.setdefault("rows", rows) or len(rows))

    reset_publisher_registry()
    reg = get_publisher_registry()
    fakes = {n: _Fake(n) for n in ("twitter", "devto")}
    for f in fakes.values():
        reg.register(f)

    await fan_out(BRIEF, platforms=["x", "devto"], campaign="launch", track=True)

    # twitter: the PUBLISHED link is the redirect
    assert "/r/launch/twitter" in fakes["twitter"].seen.text
    # dev.to: canonical stays the real, clean URL (never a redirect)
    assert "/r/" not in (fakes["devto"].seen.link or "")
    # recorded receipt for twitter = the REAL destination (UTM'd hub), not the redirect
    rows = {r["network"]: r for r in captured["rows"]}
    assert "/r/" not in rows["twitter"]["url"] and "utm_" in rows["twitter"]["url"]
    reset_publisher_registry()


def test_receipt_latest_url_resolves(tmp_path):
    s = PublishReceiptStore(tmp_path / "r.db")
    s.record(campaign="launch", network="twitter", url="https://a?utm_campaign=launch", ok=True)
    s.record(campaign="launch", network="twitter", url="https://b?utm_campaign=launch", ok=True)  # newer
    assert s.latest_url("launch", "twitter") == "https://b?utm_campaign=launch"
    assert s.latest_url("launch", "nope") is None


async def test_redirect_resolves_from_ledger_and_records_click(tmp_path, monkeypatch):
    import navig_social.social.engagement as E
    import navig_social.social.receipts as R
    from navig_social.deck_routes.redirect import handle_click_redirect

    rec = PublishReceiptStore(tmp_path / "r.db")
    rec.record(campaign="launch", network="twitter",
               url="https://cybesis.com/hub?utm_campaign=launch", ok=True)
    eng = EngagementStore(tmp_path / "e.db")
    monkeypatch.setattr(R, "get_publish_receipts", lambda: rec)
    monkeypatch.setattr(E, "get_engagement", lambda: eng)

    resp = await handle_click_redirect(_Req("launch", "twitter"))
    assert resp.status == 302
    assert resp.headers["Location"] == "https://cybesis.com/hub?utm_campaign=launch"
    assert eng.campaign_metrics("launch") == {"clicks": 1}


async def test_redirect_404_when_no_receipt(tmp_path, monkeypatch):
    import navig_social.social.receipts as R
    from navig_social.deck_routes.redirect import handle_click_redirect

    monkeypatch.setattr(R, "get_publish_receipts", lambda: PublishReceiptStore(tmp_path / "r.db"))
    resp = await handle_click_redirect(_Req("nope", "twitter"))
    assert resp.status == 404
