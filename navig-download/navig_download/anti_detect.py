"""Anti-detection helpers for the yt-dlp fetch paths (TikTok + others).

The single highest-leverage win is yt-dlp's ``impersonate`` (backed by ``curl_cffi``),
which drives requests with a **real Chrome TLS + HTTP/2 fingerprint** (JA3) that plain
``requests``/``urllib`` cannot fake — this is what gets past TLS-level bot walls. On top
of that we set a realistic rotating User-Agent + headers, and optional proxy + cookies.

The core technique: ``curl_cffi.Session(impersonate=…)`` with challenge-by-body handling.

Everything degrades gracefully: if ``curl_cffi`` / a modern yt-dlp isn't installed, the
impersonate step is skipped and the UA/proxy/cookies still apply.
"""

from __future__ import annotations

import os
import random
from typing import Any

# The SINGLE source of fallback User-Agents for this plugin — the downloader and the
# TikTok engine both draw from here via random_user_agent() (there used to be two more
# stale copies of this list). Only used when curl_cffi impersonation is unavailable; when
# it IS available, apply_anti_detect drops the UA and lets curl_cffi own a UA/sec-ch-ua
# that agrees with the TLS fingerprint. Chrome-only and current on purpose: a common,
# up-to-date Chrome UA is the least-suspicious fallback, and every major here is one
# curl_cffi can also impersonate (asserted in tests), so the UA never claims a Chrome the
# TLS layer can't produce.
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/142.0.0.0 Safari/537.36",
]

# yt-dlp impersonate target — "chrome" resolves to the newest bundled Chrome profile.
IMPERSONATE_TARGET = "chrome"


def random_user_agent() -> str:
    return random.choice(USER_AGENTS)


def impersonate_target():
    """Return a yt-dlp ImpersonateTarget if supported (needs curl_cffi), else None."""
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — old yt-dlp
        return None
    try:
        import curl_cffi  # noqa: F401, PLC0415 — the impersonate backend
    except Exception:  # noqa: BLE001 — not installed
        return None
    try:
        return ImpersonateTarget.from_str(IMPERSONATE_TARGET)
    except Exception:  # noqa: BLE001
        try:
            return ImpersonateTarget(IMPERSONATE_TARGET)
        except Exception:  # noqa: BLE001
            return None


def resolve_fetch_defaults(
    proxy: str | None = None,
    cookiefile: str | None = None,
    cookiesfrombrowser: str | None = None,
    *,
    use_session: bool = True,
) -> tuple[str | None, str | None, tuple | None]:
    """Fill unset fetch options from env, then from the vaulted session.

    - ``NAVIG_TIKTOK_PROXY`` — e.g. ``http://user:pass@host:port`` / ``socks5://…``
    - ``NAVIG_TIKTOK_COOKIES`` — path to a Netscape cookies.txt
    - ``NAVIG_TIKTOK_COOKIES_FROM_BROWSER`` — e.g. ``chrome`` (yt-dlp reads the browser jar)

    The vaulted session is the **last** resort, so anything the caller or the
    operator states explicitly still wins. Before this, `navig tt login` reached
    only the browser tier and could not unlock an age-gated ``/video/`` post —
    the login worked and the download still failed.

    *use_session* is how ``--anon`` stays anonymous: it is threaded from the CLI's
    tri-state, so an explicit "browse without my session" is honoured on the yt-dlp
    path exactly as it already is on the browser path.
    """
    proxy = proxy or os.environ.get("NAVIG_TIKTOK_PROXY") or None
    if not proxy:
        proxy = _pool_proxy_url()  # fall back to core's shared BYO proxy pool (Stage 2)
    cookiefile = cookiefile or os.environ.get("NAVIG_TIKTOK_COOKIES") or None
    cfb_env = cookiesfrombrowser or os.environ.get("NAVIG_TIKTOK_COOKIES_FROM_BROWSER") or None
    cfb = (cfb_env,) if cfb_env else None  # yt-dlp wants a tuple (browser, [profile, …])
    if use_session and not cookiefile and not cfb:
        from .tiktok.session_cookies import vaulted_cookiefile  # noqa: PLC0415

        cookiefile = vaulted_cookiefile()
    return proxy, cookiefile, cfb


def _pool_proxy_url() -> str | None:
    """Next proxy from core's shared pool, or None (no core / no proxies configured)."""
    try:
        from navig.browser.proxy import resolve_proxy_url  # noqa: PLC0415

        return resolve_proxy_url()
    except Exception:  # noqa: BLE001 — core absent or config unavailable → go direct
        return None


def apply_anti_detect(
    opts: dict[str, Any],
    *,
    proxy: str | None = None,
    cookiefile: str | None = None,
    cookiesfrombrowser: tuple | None = None,
    ua: str | None = None,
    impersonate: bool = True,
) -> dict[str, Any]:
    """Mutate a yt-dlp opts dict in place with the anti-detection settings. Returns it."""
    target = impersonate_target() if impersonate else None
    headers = dict(opts.get("http_headers") or {})

    if target is not None:
        # Coherence: curl_cffi supplies a Chrome UA + matching sec-ch-ua that agree with
        # the TLS/JA3 fingerprint. Forcing our own UA here would contradict sec-ch-ua
        # (a detection tell), so drop any pre-set UA unless the caller passed one.
        if ua:
            headers["User-Agent"] = ua
        else:
            headers.pop("User-Agent", None)
        opts["impersonate"] = target  # real Chrome TLS/HTTP2 fingerprint
    else:
        # No impersonation available → carry a plausible rotating UA as the identity.
        headers.setdefault("User-Agent", ua or random_user_agent())

    headers.setdefault("Accept-Language", "en-US,en;q=0.9")
    opts["http_headers"] = headers

    if proxy:
        opts["proxy"] = proxy
    if cookiefile:
        opts["cookiefile"] = cookiefile
    if cookiesfrombrowser:
        opts["cookiesfrombrowser"] = cookiesfrombrowser
    return opts


# ── Block-vs-empty detection ──────────────────────────────────────────────────
# A bot-wall (403/429/captcha/CF/signed-endpoint) must NOT look like a genuine empty
# result — otherwise the scraper silently reports "no comments" when it was blocked.

_BLOCK_MARKERS = (
    "http error 4", " 403", " 429", "forbidden", "too many requests",
    "captcha", "verify", "rate limit", "rate-limit", "blocked", "sign in",
    "login required", "unable to extract", "please wait", "temporarily",
)


def looks_blocked(message: str) -> bool:
    m = (message or "").lower()
    return any(marker in m for marker in _BLOCK_MARKERS)


#: A post TikTok serves only to a logged-in session — age/sensitivity gated, or
#: private. NOT a bot-wall: waiting does not clear it and a proxy does not help,
#: so it needs its own answer ("supply cookies") rather than "try again shortly".
#:
#: The phrasing that started this is `"This post may not be comfortable for some
#: audiences. Log in for access."` — note it contains **"log in"**, which neither
#: `"sign in"` nor `"login required"` in _BLOCK_MARKERS matches, so it fell through
#: to the generic branch and the bot said only "Couldn't read that video."
_LOGIN_MARKERS = (
    "log in for access", "may not be comfortable for some audiences",
    "login required", "sign in to confirm", "requires authentication",
    "private video", "this post is private",
)


def looks_login_required(message: str) -> bool:
    """True when the extractor failed because the post needs a logged-in session."""
    m = (message or "").lower()
    return any(marker in m for marker in _LOGIN_MARKERS)


#: yt-dlp received a page it could not parse. Deliberately NOT called "stale
#: extractor": the same yt-dlp build downloaded this exact post successfully and
#: then failed on it repeatedly twenty minutes later, with no version, option,
#: cache or proxy change — so the message does **not** prove the extractor is out
#: of date. It has two causes with two remedies:
#:
#:   * TikTok served a challenge/variant page — transient, retry works;
#:   * the extractor genuinely lags a site change — only an update fixes it.
#:
#: Nothing in the message distinguishes them, so the answer must cover both
#: honestly rather than assert the one that sounds most actionable. Claiming
#: "update yt-dlp" for a transient block is the same wrong-remedy bug this module
#: already fixed for age gates.
_UNREADABLE_MARKERS = (
    "unexpected response from webpage request",
    "confirm you are on the latest version",
    "please report this issue on",
)
#: NOT "unable to extract webpage video data": that phrasing is equally a bot-wall,
#: and `fetch_file` escalates a bot-wall to the browser tier, which often succeeds
#: where yt-dlp cannot. `test_fetch_file_escalates_a_bot_wall` pins that.


def looks_unreadable_response(message: str) -> bool:
    """True when yt-dlp could not parse the page — transient block OR stale extractor."""
    m = (message or "").lower()
    return any(marker in m for marker in _UNREADABLE_MARKERS)
