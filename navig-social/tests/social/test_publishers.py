"""Unit tests for the Threads / Pinterest / dev.to publishers.

Network calls need live tokens; these pin the offline behaviour — capabilities,
the auth gate, and the pure title/image helpers — so a regression in wiring is
caught without hitting an API.
"""
from __future__ import annotations

import pytest

from navig_social.social import publishers as pub
from navig_social.social.registry import get_publisher_registry, reset_publisher_registry
from navig_social.social.types import RenderedPost


def _post(text="", media=None, link=None, hashtags=None):
    p = RenderedPost(text=text, media=media or [], link=link)
    # RenderedPost is frozen and has no hashtags field; dev.to reads it defensively.
    if hashtags is not None:
        object.__setattr__(p, "hashtags", hashtags)
    return p


def test_new_publishers_are_registered():
    reset_publisher_registry()
    reg = get_publisher_registry()
    for name in ("threads", "pinterest", "devto"):
        assert reg.get(name) is not None, f"{name} not registered"
    reset_publisher_registry()


@pytest.mark.parametrize("cls,name,media", [
    (pub.ThreadsPublisher, "threads", True),
    (pub.PinterestPublisher, "pinterest", True),
    (pub.DevToPublisher, "devto", True),
])
def test_capabilities(cls, name, media):
    p = cls()
    assert p.name == name
    caps = p.capabilities
    assert caps["media"] is media
    assert "text" in caps


@pytest.mark.parametrize("cls", [pub.ThreadsPublisher, pub.PinterestPublisher, pub.DevToPublisher])
async def test_publish_requires_auth_when_unconfigured(cls, monkeypatch):
    # No token anywhere → publish short-circuits to a requires_auth receipt.
    monkeypatch.setattr(cls, "token", lambda self: None)
    receipt = await cls().publish("", _post("hello"))
    assert receipt.ok is False
    assert receipt.requires_auth is True


async def test_pinterest_needs_an_image(monkeypatch):
    monkeypatch.setattr(pub.PinterestPublisher, "token", lambda self: "tok")
    monkeypatch.setattr(pub, "get_config", lambda provider, key: "board123")
    receipt = await pub.PinterestPublisher().publish("", _post("a pin with no image"))
    assert receipt.ok is False
    assert "image" in receipt.error.lower()


async def test_pinterest_needs_a_board(monkeypatch):
    monkeypatch.setattr(pub.PinterestPublisher, "token", lambda self: "tok")
    monkeypatch.setattr(pub, "get_config", lambda provider, key: None)
    receipt = await pub.PinterestPublisher().publish(
        "", _post("pin", media=[{"url": "https://x/i.jpg"}]))
    assert receipt.ok is False
    assert "board" in receipt.error.lower()


async def test_devto_needs_a_title(monkeypatch):
    monkeypatch.setattr(pub.DevToPublisher, "token", lambda self: "key")
    receipt = await pub.DevToPublisher().publish("", _post("   "))
    assert receipt.ok is False
    assert "title" in receipt.error.lower()


def test_first_image_url():
    assert pub._first_image_url(_post(media=[{"path": "a"}, {"url": "https://x/i.jpg"}])) == "https://x/i.jpg"
    assert pub._first_image_url(_post(media=[])) is None
    assert pub._first_image_url(_post(media=[{"path": "local-only"}])) is None


def test_split_title():
    assert pub._split_title("Title\nbody line 1\nbody line 2") == ("Title", "body line 1\nbody line 2")
    assert pub._split_title("just one line") == ("just one line", "")
    # a single overlong line becomes the (truncated) title, full text as body
    long = "x" * 300
    title, body = pub._split_title(long, title_max=250)
    assert len(title) == 250
    assert body == long
    assert pub._split_title("") == ("", "")


# ── fetch_metrics (the `navig social sync` read side; mocked HTTP) ───────────


class _GetResp:
    """Async-context-manager response for a mocked ``session.get(...)``."""

    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False


class _GetSession:
    def __init__(self, payload):
        self._payload = payload
        self.requested = None

    def get(self, url, headers=None):
        self.requested = {"url": url, "headers": headers}
        return _GetResp(self._payload)

    async def close(self):
        pass


def _with_session(monkeypatch, cls, token, payload):
    from unittest.mock import AsyncMock

    session = _GetSession(payload)
    monkeypatch.setattr(cls, "token", lambda self: token)
    monkeypatch.setattr(cls, "_session", AsyncMock(return_value=session))
    return session


async def test_twitter_fetch_metrics_maps_public_metrics(monkeypatch):
    session = _with_session(
        monkeypatch, pub.TwitterPublisher, "bearer",
        {"data": {"public_metrics": {
            "impression_count": 1000, "like_count": 12, "retweet_count": 3, "reply_count": 4}}},
    )
    m = await pub.TwitterPublisher().fetch_metrics("123")
    assert m == {"views": 1000, "likes": 12, "reposts": 3, "replies": 4}
    assert "/tweets/123" in session.requested["url"]
    assert session.requested["headers"]["Authorization"] == "Bearer bearer"


async def test_twitter_fetch_metrics_none_without_token(monkeypatch):
    monkeypatch.setattr(pub.TwitterPublisher, "token", lambda self: None)
    assert await pub.TwitterPublisher().fetch_metrics("123") is None


async def test_twitter_fetch_metrics_none_on_empty_payload(monkeypatch):
    _with_session(monkeypatch, pub.TwitterPublisher, "bearer", {"data": {}})
    assert await pub.TwitterPublisher().fetch_metrics("123") is None


async def test_devto_fetch_metrics_maps_stats(monkeypatch):
    session = _with_session(
        monkeypatch, pub.DevToPublisher, "key",
        {"id": 55, "page_views_count": 800, "public_reactions_count": 20, "comments_count": 6},
    )
    m = await pub.DevToPublisher().fetch_metrics("777")
    assert m == {"views": 800, "likes": 20, "replies": 6}
    assert "/articles/777" in session.requested["url"]
    assert session.requested["headers"]["api-key"] == "key"


async def test_devto_fetch_metrics_none_on_bad_payload(monkeypatch):
    _with_session(monkeypatch, pub.DevToPublisher, "key", {"error": "not found"})  # no "id"
    assert await pub.DevToPublisher().fetch_metrics("777") is None


async def test_base_fetch_metrics_defaults_to_none():
    # A network without a read API (RedditPublisher doesn't override) returns None.
    assert await pub.RedditPublisher().fetch_metrics("x") is None


# ── Twitter publish honesty (mocked POST) ───────────────────────────────────


class _PostResp:
    """Async-context-manager response for a mocked ``session.post(...)``."""

    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False


class _PostSession:
    def __init__(self, status, payload):
        self._status = status
        self._payload = payload
        self.posted = None

    def post(self, url, json=None, data=None, headers=None):
        self.posted = {"url": url, "json": json, "data": data, "headers": headers}
        return _PostResp(self._status, self._payload)

    async def close(self):
        pass


def _with_post_session(monkeypatch, cls, token, status, payload):
    from unittest.mock import AsyncMock

    session = _PostSession(status, payload)
    monkeypatch.setattr(cls, "token", lambda self: token)
    monkeypatch.setattr(cls, "_session", AsyncMock(return_value=session))
    return session


def test_twitter_does_not_advertise_media():
    # Tweet media needs the v1.1 upload flow (not implemented) — advertising it
    # would let the composer attach an image that publish() silently drops.
    assert pub.TwitterPublisher().capabilities["media"] is False


async def test_twitter_publish_success_requires_tweet_id(monkeypatch):
    _with_post_session(
        monkeypatch, pub.TwitterPublisher, "bearer", 201, {"data": {"id": "1750000000000000000"}}
    )
    receipt = await pub.TwitterPublisher().publish("", _post("hello world"))
    assert receipt.ok is True
    assert receipt.id == "1750000000000000000"


async def test_twitter_2xx_without_id_is_failure_not_phantom_success(monkeypatch):
    # A 2xx whose body carries NO data.id (degraded / error-with-200) must NOT be
    # reported as a published tweet — that would be a phantom success.
    _with_post_session(
        monkeypatch, pub.TwitterPublisher, "bearer", 200, {"detail": "odd", "data": {}}
    )
    receipt = await pub.TwitterPublisher().publish("", _post("hello world"))
    assert receipt.ok is False
