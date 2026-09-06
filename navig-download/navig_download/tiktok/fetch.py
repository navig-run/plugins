"""Self-healing tiered comment fetch — never hard-break on a TikTok signing bump.

The orchestrator treats the browser as a **signer oracle**: TikTok's rotating
``msToken``/``X-Bogus`` can't permanently break the fast path because when Tier-A (HTTP)
is gated we auto-fall-back to a browser that signs the request itself.

    Tier-A  yt-dlp + impersonate + cookies + proxy   (fast; often gated for comments)
      ↓ block / empty-signed
    Tier-B  local stealth browser · CDP interception  (rides TikTok's own signing)
      ↓ still blocked / engine missing
    Tier-C  cloud anti-detect browser (opt-in, BYO)   (residential egress, server-side)

On a browser-tier success we record the signed endpoint's **shape** (path + param/header
NAMES, never secret values) into the signer cache — an observability + re-heal signal;
on total failure we invalidate it so the next run re-derives from the browser. The tier
functions are module-level so tests can inject fakes.
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit


def _domain(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host or "tiktok.com"


async def fetch_comments(
    url: str,
    *,
    max_comments: int = 20,
    allow_browser: bool = True,
    allow_cloud: bool = True,
    headless: bool = True,
    proxy: str | None = None,
    session_host: str | None = None,
    **fetch_opts,
) -> dict:
    """Return the best comments available across the tier ladder.

    Result: ``{"comments": [...], "tier": "A"|"B"|"C"|None, "blocked": bool,
    "comment_count": int|None, "healed": bool}``. ``healed=True`` means a browser tier
    recovered comments the HTTP tier couldn't.
    """
    domain = _domain(url)

    # ── Tier A — HTTP (yt-dlp + impersonate) ──
    a_opts = dict(fetch_opts)
    if proxy:
        a_opts.setdefault("proxy", proxy)  # the HTTP tier uses the same proxy as the browser
    a = await _tier_a(url, max_comments, **a_opts)
    if a["comments"]:
        return {"comments": a["comments"], "tier": "A", "blocked": False,
                "comment_count": a.get("comment_count"), "healed": False}
    if not a.get("blocked"):
        # Genuinely empty (comment_count 0) — no point escalating.
        return {"comments": [], "tier": "A", "blocked": False,
                "comment_count": a.get("comment_count"), "healed": False}

    # ── Tier B — local stealth browser (signer oracle) ──
    if allow_browser:
        b = await _tier_b(url, max_comments, headless=headless, proxy=proxy,
                          session_host=session_host)
        if b is not None and b.get("comments"):
            _learn(domain, b.get("signed_request"))
            return {"comments": b["comments"], "tier": "B", "blocked": True,
                    "comment_count": a.get("comment_count"), "healed": True}
        # The browser read the post but found no comments. If it also reports a count of 0,
        # trust that over Tier-A's guess: the post is genuinely empty, NOT gated/blocked.
        if b is not None and b.get("comment_count") == 0:
            return {"comments": [], "tier": "B", "blocked": False,
                    "comment_count": 0, "healed": False}

    # ── Tier C — cloud anti-detect browser (opt-in) ──
    if allow_cloud and _cloud_enabled():
        c = await _tier_c(url, max_comments, session_host=session_host)
        if c is not None and c.get("comments"):
            _learn(domain, c.get("signed_request"))
            return {"comments": c["comments"], "tier": "C", "blocked": True,
                    "comment_count": a.get("comment_count"), "healed": True}

    # Everything gated → drop any stale learned shape so next run re-derives.
    _invalidate(domain)
    return {"comments": [], "tier": None, "blocked": True,
            "comment_count": a.get("comment_count"), "healed": False}


# ── tier implementations (module-level so tests can monkeypatch) ──────────────

async def _tier_a(url: str, n: int, **opts) -> dict:
    from . import engine

    def _run() -> dict:
        try:
            m = engine.info_with_comments(url, max_comments=n, **opts)
            return {"comments": m.get("comments") or [],
                    "comment_count": m.get("comment_count"),
                    "blocked": bool(m.get("comments_blocked"))}
        except Exception:  # noqa: BLE001 — ANY Tier-A failure (block, unavailable, network)
            # means "escalate to the browser", never crash the ladder. Photo posts used to
            # be a standing member of this set; `info_with_comments` canonicalizes them to
            # the /video/ form now, so they take the fast tier like anything else.
            return {"comments": [], "comment_count": None, "blocked": True}

    return await asyncio.to_thread(_run)


async def _tier_b(url: str, n: int, *, headless: bool, proxy: str | None,
                  session_host: str | None = None) -> dict | None:
    from . import browser_fetch

    try:
        return await browser_fetch.fetch_comments(
            url, max_comments=n, headless=headless, proxy=proxy, session_host=session_host)
    except Exception:  # noqa: BLE001 — engine missing OR a browser crash/disconnect mid-fetch;
        # either way the tier failed → return None so the ladder degrades to Tier-C, not a crash.
        return None


async def _tier_c(url: str, n: int, *, session_host: str | None = None) -> dict | None:
    from . import browser_fetch

    try:
        from navig.browser.cloud import CloudBridge  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    bridge = CloudBridge.from_config()
    if bridge is None:
        return None
    try:
        return await browser_fetch.fetch_comments(
            url, max_comments=n, controller=bridge, session_host=session_host)
    except Exception:  # noqa: BLE001 — cloud engine missing OR a browser crash mid-fetch → degrade
        return None


# ── signer cache helpers ──────────────────────────────────────────────────────

def _learn(domain: str, signed_request: dict | None) -> None:
    if not signed_request:
        return
    try:
        from navig.browser.signer_cache import get_cache, shape_from_request  # noqa: PLC0415

        tpl = shape_from_request(domain, signed_request)
        if tpl is not None:
            get_cache().put(tpl)
    except Exception:  # noqa: BLE001 — learning is best-effort, never fatal
        pass


def _invalidate(domain: str) -> None:
    try:
        from navig.browser.signer_cache import get_cache  # noqa: PLC0415

        get_cache().invalidate(domain)
    except Exception:  # noqa: BLE001
        pass


def _cloud_enabled() -> bool:
    try:
        from navig.browser.cloud import cloud_enabled  # noqa: PLC0415

        return cloud_enabled()
    except Exception:  # noqa: BLE001
        return False
