"""Tier-B TikTok fetch — drive a stealth browser to read TikTok's OWN data.

Two things yt-dlp can't do, both solved here by letting a real browser do the work:

1. **Comments** — ``/api/comment/list/`` is signed (msToken/X-Bogus). We intercept the JSON
   the browser itself fetches (no signature reversing, so a signing bump can't break it).
2. **Photo posts (`/photo/…`)** — yt-dlp doesn't support them at all. The description,
   author, stats and image URLs live in the page's ``__UNIVERSAL_DATA_FOR_REHYDRATION__``
   blob (SSR'd into the HTML), which we read straight from the DOM — works for photos AND
   videos, no login needed for public posts.

Optionally restores a vaulted TikTok session first (``session_host``) so EU-gated comments
(which sit behind a login wall) come through. Uses the reusable ``navig.browser.intercept``
primitive + the ``StealthController`` (Patchright) engine, both in core.

Pure helpers (``_parse_comments`` / ``_map_stats``) are unit-tested; the live browser drive
is best-effort (TikTok's web DOM shifts) and degrades to whatever it managed to read.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

# A wide desktop window so TikTok renders its desktop layout: the video comment side-panel (with
# a 'Comments' tab that fires /api/comment/list/) only exists ≥~1024px. A narrower window gives the
# immersive full-screen layout, which has NO comment tab — the comment API never fires there.
_DESKTOP_WINDOW = (1280, 1000)
# Run the browser HEADFUL but offscreen + muted. TikTok's anti-bot returns an EMPTY comment body
# (HTTP 200, 0 bytes) to a *headless* browser even when logged in — verified — but serves real
# comments to a real window. Offscreen (large-negative position) + --mute-audio keeps that real
# window invisible and silent to the user.
_OFFSCREEN = (-2400, -2400)

# TikTok JSON endpoints worth capturing (web + app-style paths).
_COMMENT_URL_FILTERS = ["/comment/list/", "/api/comment/list", "/aweme/v1/web/comment/list"]
# Item-detail endpoints — enrich stats/images/full struct when the client fetches them.
_DETAIL_URL_FILTERS = ["/api/item/detail", "/aweme/v1/web/aweme/detail", "/api/post/item"]
_CAPTURE_FILTERS = _COMMENT_URL_FILTERS + _DETAIL_URL_FILTERS

# EU cookie-consent banner (region=EU-TTP2): a NESTED shadow-DOM web component that blocks the whole
# page from rendering — and thus the comment API from firing — until dismissed. Prefer accepting so
# the page keeps full functionality; declining is the fallback (either dismisses the banner).
_CONSENT_ACCEPT = ["Allow all", "Accept all"]
_CONSENT_DECLINE = ["Accept", "Decline optional cookies", "Decline all", "Decline"]

# A plain el.click() doesn't trip React's synthetic-event handlers, and TikTok's consent button +
# comment tab both live in (nested) shadow roots that Playwright's own click can't reliably reach.
# This walks the light DOM + every open shadow root and fires a full pointer/mouse click sequence on
# the first element matching an exact (lowercased) direct-text OR a CSS selector.
_SYNTH_CLICK_JS = r"""
(opts) => {
  const fire = (el) => { const o = {bubbles: true, cancelable: true, view: window};
    for (const t of ['pointerover','pointerdown','mousedown','pointerup','mouseup','click'])
      el.dispatchEvent(new (t.startsWith('pointer') ? PointerEvent : MouseEvent)(t, o)); };
  const texts = (opts.texts || []).map(s => s.toLowerCase());
  const attr = opts.attr || null;
  const walk = (root) => {
    for (const el of root.querySelectorAll('*')) {
      if (attr && el.matches && el.matches(attr)) { fire(el); return true; }
      if (texts.length) {
        const d = [...el.childNodes].filter(n => n.nodeType === 3)
          .map(n => n.textContent.trim()).join('').toLowerCase();
        if (d && texts.includes(d)) { fire(el.closest('button,[role=button],[role=tab]') || el); return true; }
      }
      if (el.shadowRoot && walk(el.shadowRoot)) return true;
    }
    return false;
  };
  return walk(document);
}
"""

# Scroll to paginate the comment list. Prefer the scroll container that actually holds the comment
# items (its scrollable ancestor) — the naive "deepest scrollable" is the centre video-feed column,
# not the comment side-panel, so scrolling that never advances the comment pager. Falls back to the
# deepest scroller + window when no comment item is present yet.
_SCROLL_JS = """() => {
  const anchor = document.querySelector('[data-e2e="comment-level-1"],[data-e2e="comment-list-item"]');
  if (anchor) {
    let el = anchor.parentElement;
    while (el) {
      if (el.scrollHeight - el.clientHeight > 40 && getComputedStyle(el).overflowY !== 'visible') {
        el.scrollTop = el.scrollHeight;
        return;
      }
      el = el.parentElement;
    }
  }
  let best = null, bestH = 0;
  for (const el of document.querySelectorAll('*')) {
    const oh = el.scrollHeight - el.clientHeight;
    if (oh > bestH && getComputedStyle(el).overflowY !== 'visible') { best = el; bestH = oh; }
  }
  if (best) best.scrollTop = best.scrollHeight;
  window.scrollTo(0, document.body.scrollHeight);
}"""

# Reads the item struct (description/author/stats/images) for video OR photo posts.
# Prefers the SSR'd __UNIVERSAL_DATA_FOR_REHYDRATION__ / SIGI_STATE blobs; when TikTok
# defers the struct to a client API (anonymous EU photo views), falls back to og-meta + the
# URL path, which always give the description, author handle, id and a cover image.
_UNIVERSAL_DATA_JS = r"""() => {
  const pick = (item) => {
    if (!item) return null;
    const imgs = ((item.imagePost && item.imagePost.images) || []).map(im =>
      (im.imageURL && im.imageURL.urlList && im.imageURL.urlList[0]) || '').filter(Boolean);
    return {
      id: item.id || '',
      desc: item.desc || '',
      author: (item.author && (item.author.uniqueId || item.author.nickname)) || '',
      author_name: (item.author && item.author.nickname) || '',
      stats: item.statsV2 || item.stats || {},
      images: imgs,
      is_photo: !!(item.imagePost),
      create_time: item.createTime || null,
    };
  };
  const el = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__');
  if (el) { try {
    const d = JSON.parse(el.textContent);
    const scope = d['__DEFAULT_SCOPE__'] || {};
    for (const k of Object.keys(scope)) {
      if (k.indexOf('detail') === -1) continue;
      const item = ((scope[k] || {}).itemInfo || {}).itemStruct;
      if (item) return pick(item);
    }
  } catch (e) {} }
  const sig = document.getElementById('SIGI_STATE');
  if (sig) { try {
    const d = JSON.parse(sig.textContent);
    const im = d.ItemModule || {};
    const k = Object.keys(im)[0];
    if (k) return pick(im[k]);
  } catch (e) {} }
  // Fallback: og-meta + the URL path (reliable for anonymous photo/video views).
  const meta = (p) => { const m = document.querySelector('meta[property="' + p + '"]');
                        return m ? (m.getAttribute('content') || '') : ''; };
  const path = location.pathname;
  const mm = path.match(/@([^/]+)\/(photo|video)\/(\d+)/);
  const img = meta('og:image');
  return {
    id: mm ? mm[3] : '',
    desc: meta('og:description') || '',
    author: mm ? mm[1] : '',
    author_name: (meta('og:title') || '').split('|')[0].split(' on TikTok')[0].trim(),
    stats: {},
    images: img ? [img] : [],
    is_photo: path.indexOf('/photo/') !== -1,
    create_time: null,
  };
}"""


def _parse_comments(json_bodies: list[dict], limit: int) -> list[dict]:
    """Flatten intercepted comment-list JSON into the engine's ``{text,author,likes}`` shape.

    Deduplicates by comment id (falls back to text), ranks by likes, and returns the top
    *limit*. Tolerant of missing fields and of both web + app JSON layouts.
    """
    seen: set[str] = set()
    out: list[dict] = []
    for body in json_bodies:
        if not isinstance(body, dict):
            continue
        for c in body.get("comments") or []:
            if not isinstance(c, dict):
                continue
            text = (c.get("text") or (c.get("share_info") or {}).get("desc") or "").strip()
            if not text:
                continue
            cid = str(c.get("cid") or c.get("id") or text)
            if cid in seen:
                continue
            seen.add(cid)
            user = c.get("user") or {}
            author = (user.get("unique_id") or user.get("nickname")
                      or c.get("author") or "").strip()
            likes = int(c.get("digg_count") or c.get("like_count") or 0)
            out.append({"text": text, "author": author, "likes": likes})
    out.sort(key=lambda x: x["likes"], reverse=True)
    return out[:limit]


def _map_stats(stats: dict) -> dict:
    """Map TikTok's stats blob (diggCount/playCount/… ; strings in statsV2) to our shape."""
    def g(*keys):
        for k in keys:
            v = (stats or {}).get(k)
            if v not in (None, ""):
                try:
                    return int(v)
                except (TypeError, ValueError):
                    continue
        return None
    return {
        "view_count": g("playCount"),
        "like_count": g("diggCount"),
        "comment_count": g("commentCount"),
        "repost_count": g("shareCount"),
        "save_count": g("collectCount"),
    }


def _detail_from_bodies(bodies: list[dict]) -> dict | None:
    """Pull a full item struct from intercepted item-detail JSON (web OR app shape)."""
    for body in bodies:
        if not isinstance(body, dict):
            continue
        item = (body.get("itemInfo") or {}).get("itemStruct")  # web
        if isinstance(item, dict):
            imgs = [((im.get("imageURL") or {}).get("urlList") or [None])[0]
                    for im in ((item.get("imagePost") or {}).get("images") or [])]
            author = item.get("author") or {}
            return {
                "id": item.get("id") or "", "desc": item.get("desc") or "",
                "author": author.get("uniqueId") or author.get("nickname") or "",
                "author_name": author.get("nickname") or "",
                "stats": item.get("statsV2") or item.get("stats") or {},
                "images": [i for i in imgs if i], "is_photo": bool(item.get("imagePost")),
                "create_time": item.get("createTime"),
            }
        aw = body.get("aweme_detail")  # app
        if isinstance(aw, dict):
            stat = aw.get("statistics") or {}
            imgs = [((im.get("display_image") or {}).get("url_list") or [None])[0]
                    for im in ((aw.get("image_post_info") or {}).get("images") or [])]
            author = aw.get("author") or {}
            return {
                "id": aw.get("aweme_id") or "", "desc": aw.get("desc") or "",
                "author": author.get("unique_id") or author.get("nickname") or "",
                "author_name": author.get("nickname") or "",
                "stats": {"diggCount": stat.get("digg_count"),
                          "commentCount": stat.get("comment_count"),
                          "playCount": stat.get("play_count"),
                          "shareCount": stat.get("share_count")},
                "images": [i for i in imgs if i], "is_photo": bool(aw.get("image_post_info")),
                "create_time": aw.get("create_time"),
            }
    return None


def _merge_raw(base: dict | None, detail: dict | None) -> dict | None:
    """Overlay non-empty *detail* fields onto *base* (the intercepted struct wins)."""
    if not detail:
        return base
    merged = dict(base or {})
    for k, v in detail.items():
        if v not in (None, "", [], {}):
            merged[k] = v
    return merged


def _build_meta(url: str, raw: dict | None) -> dict:
    """Project the JS-extracted item struct into the same shape ``engine._summarize`` uses."""
    raw = raw or {}
    stats = _map_stats(raw.get("stats") or {})
    return {
        "url": url,
        "id": raw.get("id") or "",
        "description": (raw.get("desc") or "").strip(),
        "uploader": (raw.get("author") or "").strip(),
        "uploader_name": (raw.get("author_name") or "").strip(),
        "images": raw.get("images") or [],
        "is_photo": bool(raw.get("is_photo")),
        "timestamp": raw.get("create_time"),
        **stats,
    }


async def _make_controller(headless: bool, proxy: str | None, controller):
    """Return ``(controller, owns_it, engine_name)`` — build a StealthController if none given.

    The FETCH tier stays on Chromium (Patchright) on purpose: it reliably reads a post's
    SSR blob + intercepts the signed item-detail/comment JSON. A Camoufox/Firefox fetch was
    tested and came back EMPTY on the same public post the Chromium fetch read fully, so don't
    naively swap the default here (login uses Camoufox for stealth; getting the Camoufox fetch
    to intercept is a separate follow-up). Callers may still pass their own ``controller``.
    """
    if controller is not None:
        return controller, False, type(controller).__name__
    try:
        from navig.browser.stealth import StealthConfig, StealthController
    except Exception as exc:  # noqa: BLE001
        raise TikTokBrowserUnavailable(
            "the stealth browser engine is unavailable — install patchright "
            "(`pip install patchright && patchright install chromium`)."
        ) from exc
    if proxy is None:
        try:
            from navig.browser.proxy import resolve_proxy_url

            proxy = resolve_proxy_url()
        except Exception:  # noqa: BLE001
            proxy = None
    # Force the wide desktop layout so the comment side-panel (and its 'Comments' tab) render, and
    # run HEADFUL-offscreen-muted (headless gets an empty comment body from TikTok's anti-bot).
    # Older cores without these StealthConfig fields still work — fall back to a plain config.
    try:
        cfg = StealthConfig(
            headless=headless, proxy=proxy, window_size=_DESKTOP_WINDOW,
            window_position=(None if headless else _OFFSCREEN), mute_audio=not headless,
        )
    except TypeError:  # noqa: PERF203 — pre-window_size/position core
        cfg = StealthConfig(headless=headless, proxy=proxy)
    return StealthController(cfg), True, "browser"


async def _restore_session(controller, host: str) -> bool:
    """Restore a vaulted storageState for *host* onto the live context (best-effort)."""
    try:
        from navig.vault.sessions import get_session

        st = get_session(host)
        state = st.storage_state if st else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("[browser_fetch] session lookup failed: %s", exc)
        return False
    if not state:
        return False
    ctx = getattr(controller, "context", None)
    if ctx is None:
        return False
    try:
        cookies = state.get("cookies") or []
        if cookies:
            await ctx.add_cookies(cookies)
        logger.info("[browser_fetch] restored %d cookie(s) for %s", len(cookies), host)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("[browser_fetch] session restore failed: %s", exc)
        return False


async def fetch_post(
    url: str,
    *,
    max_comments: int = 20,
    headless: bool = False,
    proxy: str | None = None,
    rounds: int = 8,
    controller: object | None = None,
    session_host: str | None = None,
) -> dict:
    """Read a TikTok post (video OR ``/photo/``) via the browser: description + comments.

    Runs a HEADFUL browser offscreen+muted by default (``headless=False``): TikTok's anti-bot
    returns an empty comment body to a headless browser, so comments require a real window (kept
    invisible via an offscreen position). Pass ``headless=True`` for a faster description-only read.

    Returns ``{"meta": {...description, stats, images, is_photo...}, "comments": [...],
    "captured": <int>, "engine": <str>, "signed_request": <shape|None>}``. Optionally
    restores a vaulted session (``session_host``, e.g. ``"tiktok.com"``) so login-gated
    comments come through. Raises ``TikTokBrowserUnavailable`` if no browser engine exists.
    """
    try:
        from navig.browser.intercept import capture_json
    except Exception as exc:  # noqa: BLE001
        raise TikTokBrowserUnavailable(
            "the browser engine is unavailable — install patchright."
        ) from exc

    # Comments require a real (headful) window — TikTok serves an empty comment body to a headless
    # browser. A description-only read (max_comments <= 0) can stay headless (faster, no window).
    # So a caller that asks for comments always gets the headful path regardless of `headless`.
    effective_headless = headless and max_comments <= 0
    controller, owns, engine_name = await _make_controller(effective_headless, proxy, controller)
    await controller.start()
    try:
        page = controller.page
        if session_host:
            await _restore_session(controller, session_host)

        # Signal the moment the comment API fires — activation succeeded (the request only goes out
        # once the 'Comments' tab is active). Lets _go stop re-clicking as soon as it's confirmed,
        # including genuinely-empty videos where no comment items ever render (previously a ~15s
        # retry waste). Reset per navigation so a reused controller doesn't inherit a stale True.
        comment_api_fired = {"v": False}

        def _watch_comment_api(resp: object) -> None:
            try:
                url_l = (getattr(resp, "url", "") or "").lower()
            except Exception:  # noqa: BLE001
                return
            if any(f in url_l for f in _COMMENT_URL_FILTERS):
                comment_api_fired["v"] = True

        page.on("response", _watch_comment_api)

        async def _go():
            await page.goto(url, wait_until="domcontentloaded")
            if max_comments <= 0:
                return  # description-only read — no need to open the comment panel
            # The desktop side-panel + Comments tab render a few seconds in; dismiss consent (which
            # reflows the panel) then select the 'Comments' tab so the signed /api/comment/list/
            # request fires (the panel defaults to 'You may like', so comments never load otherwise).
            # Activate the tab, then wait WELL-SPACED — a rapid re-click toggles the tab back to
            # 'You may like' and loses the load; break the moment the API fires or items render.
            await asyncio.sleep(4.0)
            await _dismiss_consent(page)
            await asyncio.sleep(2.0)
            for _ in range(3):
                await _activate_comments(page)
                activated = False
                for _ in range(4):  # give this click ~4s to take effect before deciding to re-click
                    await asyncio.sleep(1.0)
                    if comment_api_fired["v"] or await _comments_rendered(page):
                        activated = True
                        break
                if activated:
                    break

        async def _scroll():
            await page.evaluate(_SCROLL_JS)

        # Stop scrolling as soon as we have enough comments OR TikTok signals the last page
        # (has_more falsy) — so a small fetch finishes in ~2 pages instead of all `rounds`, and a
        # large one isn't capped short. `rounds` becomes a safety cap, scaled to the ask.
        def _stop(collected: list[dict]) -> bool:
            if max_comments <= 0:
                return False
            cbodies = [it.get("json") for it in collected
                       if isinstance(it.get("json"), dict) and it["json"].get("comments") is not None]
            if not cbodies:
                return False
            if any(not b.get("has_more") for b in cbodies):
                return True  # reached the last comment page
            return len(_parse_comments(cbodies, max_comments + 1)) >= max_comments

        round_cap = rounds if max_comments <= 0 else max(rounds, min(60, (max_comments // 4) + 5))
        items = await capture_json(
            page, _CAPTURE_FILTERS,
            trigger=_go, scroller=_scroll, rounds=round_cap, read_only=True, with_meta=True,
            stop_when=_stop,
        )
        # Description/author/id/cover come from the SSR blob or og-meta+URL (always present);
        # stats/full images are enriched from any intercepted item-detail JSON.
        raw = None
        try:
            raw = await page.evaluate(_UNIVERSAL_DATA_JS)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[browser_fetch] universal-data read failed: %s", exc)

        bodies = [it["json"] for it in items]
        comments = _parse_comments(bodies, max_comments)
        signed = next((it["request"] for it in items
                       if it.get("request") and "comment" in (it["request"].get("url") or "")),
                      None)
        meta = _build_meta(url, _merge_raw(raw, _detail_from_bodies(bodies)))
        meta["comment_captured"] = len(comments)
        return {"meta": meta, "comments": comments, "captured": len(bodies),
                "engine": engine_name, "signed_request": signed}
    finally:
        # Drop the watcher so a reused controller (owns=False, e.g. a photo batch) doesn't stack a
        # listener per call. Harmless if the page/controller is already gone.
        try:
            page.remove_listener("response", _watch_comment_api)
        except Exception:  # noqa: BLE001
            pass
        if owns:
            await controller.stop()


async def fetch_comments(
    url: str,
    *,
    max_comments: int = 20,
    headless: bool = False,
    proxy: str | None = None,
    rounds: int = 8,
    controller: object | None = None,
    session_host: str | None = None,
) -> dict:
    """Comments-focused wrapper over :func:`fetch_post` (keeps the Tier-B/orchestrator shape).

    Returns ``{"comments", "captured", "engine", "signed_request"}``.
    """
    res = await fetch_post(url, max_comments=max_comments, headless=headless, proxy=proxy,
                           rounds=rounds, controller=controller, session_host=session_host)
    return {"comments": res["comments"], "captured": res["captured"],
            "engine": res["engine"], "signed_request": res["signed_request"],
            "comment_count": (res.get("meta") or {}).get("comment_count")}


async def _synth_click(page: object, *, texts: list[str] | None = None,
                       attr: str | None = None) -> bool:
    """Fire a React-honored click on the first shadow-walked element matching *texts*/*attr*."""
    try:
        return bool(await page.evaluate(_SYNTH_CLICK_JS, {"texts": texts or [], "attr": attr}))
    except Exception:  # noqa: BLE001 — page/JS hiccup must never wedge the fetch
        return False


async def _dismiss_consent(page: object) -> bool:
    """Dismiss the EU cookie-consent banner (nested shadow-DOM web component) so the comment
    panel can render. Prefer 'Allow all'; fall back to declining. Best-effort, returns True if
    a control was clicked."""
    for group in (_CONSENT_ACCEPT, _CONSENT_DECLINE):
        if await _synth_click(page, texts=group):
            logger.info("[browser_fetch] dismissed cookie consent")
            return True
    return False


# The visible 'Comments' tab label is what a real mouse click must hit (the [data-e2e="comments"]
# tab wrapper is 0×0, unclickable by coordinates). Its TEXT is localized — the label follows the
# account's UI language, not the video's — so match a set of translations for the major TikTok UI
# languages. Lowercased for a case-insensitive compare. A non-listed language still has the
# data-e2e synthetic fallback in _activate_comments.
_COMMENTS_TAB_LABELS = [
    "comments", "comentarios", "comentários", "commentaires", "kommentare", "commenti", "komentar",
    "yorumlar", "комментарии", "коментарі", "التعليقات", "コメント", "댓글", "评论", "評論", "留言",
    "bình luận", "ความคิดเห็น", "reacties", "komentarze", "komentáře", "kommentarer", "σχόλια",
    "mga komento", "टिप्पणियां", "commentaar",
]

# Locate the *visible* 'Comments' tab label (matching any known translation) and return its centre,
# walking open shadow roots. A real mouse click here is the most reliable trigger — firing synthetic
# events on the 0×0 wrapper is flaky under Patchright.
_COMMENTS_TAB_BOX_JS = r"""(labels) => {
  const walk = (root) => {
    for (const el of root.querySelectorAll('*')) {
      const d = [...el.childNodes].filter(n => n.nodeType === 3)
        .map(n => n.textContent.trim()).join('').toLowerCase();
      if (d && labels.includes(d)) {
        const r = el.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) return {x: r.x + r.width / 2, y: r.y + r.height / 2};
      }
      if (el.shadowRoot) { const res = walk(el.shadowRoot); if (res) return res; }
    }
    return null;
  };
  return walk(document);
}"""


async def _activate_comments(page: object) -> bool:
    """Select the desktop 'Comments' tab so the signed /api/comment/list/ request fires.

    TikTok's video side-panel defaults to the 'You may like' tab, so comments never load until the
    'Comments' tab is picked. Clicks the visible label with a real mouse click (mimics a user — the
    reliable trigger), matching the label across languages; falls back to a synthetic click on the
    language-independent ``[data-e2e="comments"]`` tab, then the label. No-op (returns False) on the
    immersive/narrow layout, which has no tab — harmless."""
    try:
        box = await page.evaluate(_COMMENTS_TAB_BOX_JS, _COMMENTS_TAB_LABELS)
        if box:
            await page.mouse.click(box["x"], box["y"])
            logger.info("[browser_fetch] clicked the Comments tab")
            return True
    except Exception:  # noqa: BLE001 — fall through to the synthetic path
        pass
    # Language-independent fallback: the stable data-e2e tab id (0×0 → synthetic dispatch).
    if await _synth_click(page, attr='[data-e2e="comments"][role="tab"]'):
        logger.info("[browser_fetch] activated the Comments tab (data-e2e)")
        return True
    if await _synth_click(page, texts=_COMMENTS_TAB_LABELS):
        logger.info("[browser_fetch] activated the Comments tab (label)")
        return True
    return False


async def _comments_rendered(page: object) -> bool:
    """True once comment items are in the DOM — confirms the Comments tab actually activated."""
    try:
        n = await page.evaluate(
            '() => document.querySelectorAll(\'[data-e2e="comment-level-1"],'
            '[data-e2e="comment-list-item"],[class*="CommentItem"]\').length')
        return bool(n)
    except Exception:  # noqa: BLE001
        return False


class TikTokBrowserUnavailable(RuntimeError):
    """The stealth browser engine (Patchright/Playwright) is not installed."""
