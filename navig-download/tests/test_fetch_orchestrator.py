"""Stage 8 — the self-healing tier ladder (A→B→C) with injected fakes."""

from __future__ import annotations

import pytest

from navig_download.tiktok import fetch


@pytest.fixture
def spy(monkeypatch):
    """Record tier calls + learn/invalidate; return a namespace to configure results."""
    calls = {"a": 0, "b": 0, "c": 0, "learned": [], "invalidated": []}

    async def default_a(url, n, **o):
        calls["a"] += 1
        return {"comments": [], "comment_count": 0, "blocked": False}

    async def default_b(url, n, **o):
        calls["b"] += 1
        return None

    async def default_c(url, n, **o):
        calls["c"] += 1
        return None

    state = {"a": default_a, "b": default_b, "c": default_c, "cloud": False}

    async def a(url, n, **o):
        return await state["a"](url, n, **o)

    async def b(url, n, **o):
        return await state["b"](url, n, **o)

    async def c(url, n, **o):
        return await state["c"](url, n, **o)

    monkeypatch.setattr(fetch, "_tier_a", a)
    monkeypatch.setattr(fetch, "_tier_b", b)
    monkeypatch.setattr(fetch, "_tier_c", c)
    monkeypatch.setattr(fetch, "_cloud_enabled", lambda: state["cloud"])
    monkeypatch.setattr(fetch, "_learn", lambda d, s: calls["learned"].append((d, s)))
    monkeypatch.setattr(fetch, "_invalidate", lambda d: calls["invalidated"].append(d))
    return calls, state


async def _run(**kw):
    return await fetch.fetch_comments("https://www.tiktok.com/@x/video/1", **kw)


async def test_tier_a_success_stops_early(spy):
    calls, state = spy

    async def a_ok(url, n, **o):
        calls["a"] += 1
        return {"comments": [{"text": "hi", "likes": 3, "author": "a"}],
                "comment_count": 1, "blocked": False}

    state["a"] = a_ok
    res = await _run()
    assert res["tier"] == "A" and res["healed"] is False
    assert calls["b"] == 0 and calls["c"] == 0  # no escalation


async def test_genuine_empty_does_not_escalate(spy):
    calls, _ = spy  # default_a returns empty + blocked False
    res = await _run()
    assert res["tier"] == "A" and res["comments"] == []
    assert calls["b"] == 0  # empty ≠ blocked → no browser


async def test_blocked_escalates_to_browser_and_learns(spy):
    calls, state = spy

    async def a_blocked(url, n, **o):
        calls["a"] += 1
        return {"comments": [], "comment_count": 500, "blocked": True}

    async def b_ok(url, n, **o):
        calls["b"] += 1
        return {"comments": [{"text": "x", "likes": 1, "author": "u"}],
                "signed_request": {"url": "https://t/api/comment/list/?msToken=X"}}

    state["a"], state["b"] = a_blocked, b_ok
    res = await _run()
    assert res["tier"] == "B" and res["healed"] is True
    assert res["comment_count"] == 500
    assert calls["learned"]  # signer shape recorded
    assert calls["c"] == 0   # didn't need cloud


async def test_falls_through_to_cloud_when_browser_empty(spy):
    calls, state = spy

    async def a_blocked(url, n, **o):
        return {"comments": [], "comment_count": 9, "blocked": True}

    async def c_ok(url, n, **o):
        calls["c"] += 1
        return {"comments": [{"text": "cloud", "likes": 2, "author": "z"}],
                "signed_request": {"url": "https://t/api/comment/list/"}}

    state["a"], state["cloud"] = a_blocked, True
    state["c"] = c_ok
    res = await _run()
    assert res["tier"] == "C" and res["healed"] is True
    assert calls["learned"]


async def test_all_blocked_invalidates(spy):
    calls, state = spy

    async def a_blocked(url, n, **o):
        return {"comments": [], "comment_count": 9, "blocked": True}

    state["a"], state["cloud"] = a_blocked, True  # cloud on but c returns None (default)
    res = await _run()
    assert res["tier"] is None and res["blocked"] is True
    assert calls["invalidated"] == [fetch._domain("https://www.tiktok.com/@x/video/1")]


async def test_allow_browser_false_skips_escalation(spy):
    calls, state = spy

    async def a_blocked(url, n, **o):
        return {"comments": [], "comment_count": 9, "blocked": True}

    state["a"] = a_blocked
    res = await _run(allow_browser=False)
    assert res["tier"] is None
    assert calls["b"] == 0


def test_domain_strips_www():
    assert fetch._domain("https://www.tiktok.com/@x/video/1") == "tiktok.com"
    assert fetch._domain("https://m.tiktok.com/v/2") == "m.tiktok.com"
