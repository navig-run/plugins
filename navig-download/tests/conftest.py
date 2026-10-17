"""Shared isolation for the navig-download suite.

The TikTok engine keeps three PROCESS-GLOBAL caches, all of them deliberate:
resolved share links, post metadata, and downloaded clips. They exist because one
shared link is read up to five times — the card plus each button — and every
extra request is another chance for TikTok to decide we look like a bot.

In a test process they are a cross-test channel instead. Three cases reused the
same URL with different fake yt-dlp data and the second read the first's answer;
that is a stale-state bug wearing a passing test's clothes, and it only appears
once the cache is real. Cleared around every test so a case can never inherit an
answer it did not ask for.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_engine_caches():
    """Drop the engine's per-process caches before and after each test.

    Import-guarded: the suite must still run in a checkout where the optional
    engine dependencies are missing, and a conftest that raises takes every test
    with it.
    """
    def _clear() -> None:
        try:
            from navig_download.tiktok import engine
        except Exception:  # noqa: BLE001 — nothing imported, nothing to reset
            return
        for name in ("_info_cache", "_resolved"):
            cache = getattr(engine, name, None)
            if isinstance(cache, dict):
                cache.clear()

    _clear()
    yield
    _clear()


@pytest.fixture(autouse=True)
def _no_real_http(monkeypatch):
    """No test in this suite reaches tiktok.com by accident.

    `_get_html` is the one seam `read_post_http` fetches through, and it became
    reachable from a new direction: `info()` now falls back to the SSR reader when
    yt-dlp is refused, so *any* test that makes `_ydl` throw would have made a live
    request — `test_a_failed_read_is_not_cached` did, and its outcome then depended
    on whether TikTok answered.

    Returns empty HTML rather than raising, so the fallback resolves to None and
    the caller's real failure surfaces unchanged. A test that wants the reader
    stubs `_get_html` itself; monkeypatch applies that override on top of this one.
    """
    try:
        from navig_download.tiktok import engine
    except Exception:  # noqa: BLE001 — optional deps absent; nothing to guard
        return
    monkeypatch.setattr(engine, "_get_html", lambda url, **kw: "", raising=False)


@pytest.fixture(autouse=True)
def _no_vaulted_session(monkeypatch):
    """No test's outcome depends on whether the developer is logged into TikTok.

    `resolve_fetch_defaults` now falls back to the vaulted session, so on a machine
    with a saved login it returns a cookiefile where it used to return None — and
    `test_resolve_fetch_defaults_empty` started failing for the operator and nobody
    else. That is a test reading the machine rather than the contract.

    Neutralised by default; a test that wants the bridge stubs it explicitly.
    """
    try:
        from navig_download.tiktok import session_cookies
    except Exception:  # noqa: BLE001 — optional deps absent; nothing to neutralise
        return
    monkeypatch.setattr(session_cookies, "vaulted_cookiefile", lambda *a, **kw: None)
    monkeypatch.setattr(session_cookies, "_cached_path", None, raising=False)
