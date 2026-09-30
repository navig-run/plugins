"""fan-out — per-platform adaptation + UTM + publish routing (no live calls)."""

from __future__ import annotations

import pytest

from navig_social.social.base import BasePublisher
from navig_social.social.fanout import Brief, fan_out, preview, utm
from navig_social.social.registry import get_publisher_registry, reset_publisher_registry
from navig_social.social.types import PublishReceipt

BRIEF = {
    "title": "Ship faster with NAVIG",
    "body": "NAVIG turns intent into completed work across real infrastructure. Read the deep dive.",
    "url": "https://cybesis.com/hub/navig",
    "campaign": "weekly",
}
PLATFORMS = ["x", "facebook", "devto", "telegram"]


def test_utm_appends_source_medium_campaign():
    u = utm("https://cybesis.com/x", "twitter", "weekly launch!")
    assert "utm_source=twitter" in u
    assert "utm_medium=social" in u
    assert "utm_campaign=weekly-launch" in u  # slugified


def test_preview_adapts_each_platform():
    by = {r.platform: r for r in preview(BRIEF, platforms=PLATFORMS, campaign="weekly")}
    # X fits in 280
    assert by["x"].chars <= 280
    # UTM on the social links
    for p in ("x", "facebook", "telegram"):
        assert "utm_source=" in (by[p].link or ""), f"{p} link missing UTM"
    # dev.to canonical stays CLEAN (no UTM) and equals the hub URL
    assert "utm_" not in (by["devto"].link or "")
    assert by["devto"].link == BRIEF["url"]


class _Fake(BasePublisher):
    def __init__(self, name: str) -> None:
        self.name = name
        self.seen = None

    def is_configured(self) -> bool:
        return True

    async def publish(self, target, post):
        self.seen = post
        return PublishReceipt.success(self.name, target, id="1")


async def test_fan_out_routes_adapted_payload_to_each_publisher():
    reset_publisher_registry()
    reg = get_publisher_registry()  # rebuilds with builtins; we overwrite the 4 we test
    fakes = {n: _Fake(n) for n in ("twitter", "facebook", "devto", "telegram")}
    for f in fakes.values():
        reg.register(f)

    receipts = await fan_out(BRIEF, platforms=PLATFORMS, campaign="weekly", record=False)
    assert receipts and all(r.ok for r in receipts)

    # twitter: a ≤280 hook carrying the UTM'd link
    tw = fakes["twitter"].seen
    assert tw is not None and len(tw.text) <= 280
    assert "utm_source=twitter" in tw.text
    # dev.to: canonical link is clean
    assert "utm_" not in (fakes["devto"].seen.link or "")
    reset_publisher_registry()


def test_brief_validation():
    assert Brief.from_dict({"title": "Hi"}).title == "Hi"
    with pytest.raises(ValueError):
        Brief.from_dict({})  # empty
    with pytest.raises(ValueError):
        Brief.from_dict({"title": "  ", "body": ""})  # blank
    with pytest.raises(TypeError):
        Brief.from_dict([1, 2, 3])  # non-object JSON


def test_brief_rejects_non_string_fields():
    # These previously escaped as AttributeError deep in _adapt/_slug on the live
    # publish path; now they're a clean error at the boundary.
    for bad in (
        {"title": "x", "url": 123},
        {"title": "x", "campaign": 123},
        {"title": "x", "image": 5.0},
        {"title": "x", "per_platform_overrides": ["y"]},
        {"title": ["a"]},  # non-string title
    ):
        with pytest.raises((ValueError, TypeError)):
            Brief.from_dict(bad)


async def test_fan_out_dedups_alias_collision():
    reset_publisher_registry()
    reg = get_publisher_registry()
    fake = _Fake("twitter")
    reg.register(fake)
    # `x` and `twitter` both resolve to the twitter network → publish exactly once.
    receipts = await fan_out(BRIEF, platforms=["x", "twitter"], campaign="c", record=False)
    assert len(receipts) == 1
    reset_publisher_registry()


async def test_fan_out_empty_platforms_publishes_nothing():
    # An explicit empty list is NOT "use defaults" — it publishes to nothing.
    receipts = await fan_out(BRIEF, platforms=[], campaign="c")
    assert receipts == []


async def test_fan_out_records_receipts_to_the_ledger(monkeypatch):
    # A live fan-out records each network's receipt (campaign slug + UTM'd URL).
    import navig_social.social.receipts as R

    captured = {}

    def _rec(rows):
        captured["rows"] = rows
        return len(rows)

    monkeypatch.setattr(R, "record_receipts", _rec)
    reset_publisher_registry()
    get_publisher_registry().register(_Fake("twitter"))

    await fan_out(BRIEF, platforms=["x"], campaign="weekly launch!", record=True)
    rows = captured.get("rows")
    assert rows and rows[0]["network"] == "twitter"
    assert rows[0]["campaign"] == "weekly-launch"  # slugified → the utm_campaign join key
    assert "utm_" in (rows[0]["url"] or "")
    reset_publisher_registry()
