"""Cross-store giveaway sourcing — the free games that AREN'T on Epic or Steam.

navig sources Epic (``sources/epic.py``, auto-claimable) and Steam free-to-keep
(``sources/steam.py``) natively and authoritatively. Everything else — itch.io,
GOG, IndieGala, Ubisoft, Stove … — has no single official feed, which is exactly
the gap community trackers (r/FreeGameFindings and its Telegram mirrors) fill.

Rather than scrape a relay (those mirrors are IFTTT reposts with shortened links,
and Reddit's JSON API now 403s unauthenticated), we read **GamerPower's public,
keyless giveaway API** — purpose-built, structured, and cross-store.

Entries for stores we already source natively are **dropped**, so the Free tab
never shows the same game twice; this feed only fills the gap. Nothing here is
auto-claimable: these are surfaced (and notified) to grab in their own store.
"""

from __future__ import annotations

import logging
import re
import time

from ..models import FreeGame

_log = logging.getLogger(__name__)

API_URL = "https://www.gamerpower.com/api/giveaways"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) navig-games"}

#: Stores navig sources natively (better data, and Epic is auto-claimable) — an
#: entry for one of these is dropped so the Free tab can't show a duplicate.
NATIVE_STORES = ("epic", "steam")

_CACHE: tuple[float, list] | None = None
_TTL = 300.0


def clear_cache() -> None:
    """Drop the giveaway cache (tests / a forced refresh)."""
    global _CACHE
    _CACHE = None


def _store_of(platforms: str, title: str) -> str:
    """Normalise a store id. The feed puts the store in ``platforms`` *or* in the
    title's trailing ``(Store)`` tag, so both are inspected."""
    p = (platforms or "").lower()
    t = (title or "").lower()
    for needle, store in (
        ("epic games", "epic"),
        ("steam", "steam"),
        ("gog", "gog"),
        ("itch", "itch"),
        ("ubisoft", "ubisoft"),
        ("origin", "origin"),
        ("amazon", "amazon"),
        ("indiegala", "indiegala"),
        ("stove", "stove"),
    ):
        if needle in p or needle in t:
            return store
    return "drm-free" if "drm-free" in p else "pc"


def _clean_title(raw: str) -> str:
    """``"Madness Inside (itch.io) Giveaway"`` → ``"Madness Inside"``."""
    t = re.sub(r"\s*giveaway\s*$", "", str(raw or "").strip(), flags=re.I)
    t = re.sub(r"\s*\([^)]*\)\s*$", "", t).strip()
    return t or str(raw or "").strip()


def _ends_at(value: str) -> str:
    """The feed uses ``"N/A"`` for open-ended giveaways."""
    v = str(value or "").strip()
    if not v or v.upper() == "N/A":
        return ""
    return v.replace(" ", "T")  # "2026-07-19 23:59:00" → ISO-ish


def _one_line(value) -> str:
    """The feed's instructions are a numbered list with ragged whitespace/newlines —
    collapse it so the UI can render it as one honest 'what it takes' line."""
    return " ".join(str(value or "").split())


def _to_game(item: dict) -> "FreeGame | None":
    """One feed entry → a FreeGame, or None if it isn't a PC game we should show."""
    if str(item.get("status") or "").lower() != "active":
        return None
    if str(item.get("type") or "").lower() != "game":
        return None  # DLC / loot / beta keys are not free *games*
    platforms = str(item.get("platforms") or "")
    if "pc" not in platforms.lower():
        return None  # mobile/VR-only giveaways aren't for the PC library
    title = str(item.get("title") or "")
    store = _store_of(platforms, title)
    if store in NATIVE_STORES:
        return None  # sourced natively — never duplicate it here
    url = str(item.get("open_giveaway_url") or item.get("gamerpower_url") or "")
    if not url:
        return None
    return FreeGame(
        store=store,
        title=_clean_title(title),
        offer_id=str(item.get("id") or ""),
        url=url,
        image=str(item.get("thumbnail") or item.get("image") or ""),
        description=str(item.get("description") or ""),
        instructions=_one_line(item.get("instructions")),
        original_price=str(item.get("worth") or "").replace("N/A", ""),
        ends_at=_ends_at(item.get("end_date")),
        is_current=True,
    )


def fetch(*, use_cache: bool = True, ttl: float = _TTL, timeout: int = 20) -> list[FreeGame]:
    """Active cross-store PC game giveaways (excluding Epic/Steam — see module docs).

    Never raises: a sourcing failure yields an empty list rather than breaking the
    Free feed.
    """
    global _CACHE
    if use_cache and _CACHE and _CACHE[0] > time.time():
        return list(_CACHE[1])

    try:
        import requests

        resp = requests.get(
            API_URL, params={"platform": "pc", "type": "game"},
            headers=_HEADERS, timeout=timeout,
        )
        resp.raise_for_status()
        items = resp.json()
        if not isinstance(items, list):
            items = []
    except Exception as exc:  # noqa: BLE001
        _log.warning("giveaways: fetch failed (%s)", exc)
        return []

    games = [g for g in (_to_game(i) for i in items if isinstance(i, dict)) if g]
    _CACHE = (time.time() + ttl, games)
    _log.info("giveaways: %d cross-store giveaway(s) from %d entries", len(games), len(items))
    return list(games)
