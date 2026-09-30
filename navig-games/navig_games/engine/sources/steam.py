"""Steam deals sourcing — watch your wishlist (+ a manual watchlist) for price
drops and free-to-keep giveaways. Public Steam APIs, no key, no login.

* SteamID64 is auto-detected from the installed Steam (loginusers.vdf).
* Wishlist appids come from ``IWishlistService/GetWishlist/v1`` (keyless).
* Prices come from the store ``appdetails`` endpoint (keyless).

A *deal* is a watched app that is either FREE-to-keep right now (a normally-paid
game at 100% off / $0 — not a permanently-free game) or discounted at/above the
threshold.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass

from ..models import FreeGame

_log = logging.getLogger(__name__)

WISHLIST_URL = "https://api.steampowered.com/IWishlistService/GetWishlist/v1/"
APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
SEARCH_URL = "https://store.steampowered.com/search/results/"
APP_URL = "https://store.steampowered.com/app/{appid}/"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) navig-games"}
_MAX_APPS = 80  # politeness cap on live price lookups per run


@dataclass
class SteamDeal:
    appid: int
    name: str
    discount_pct: int
    final_cents: int
    initial_cents: int
    currency: str
    final_formatted: str
    is_free: bool  # free-to-keep promo (a normally-paid game now $0)
    on_wishlist: bool
    url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def key(self) -> str:
        return f"steam:{self.appid}"


def resolve_steamid64() -> str | None:
    """Config override, else the most-recent user from the installed Steam."""
    from .. import settings

    override = settings.get("steamid64")
    if override:
        return str(override)
    try:
        from ..library.steam import login_users, steam_path

        sp = steam_path()
        if sp is None:
            return None
        users = login_users(sp)
        if not users:
            return None
        for u in users:
            if u["most_recent"]:
                return u["steamid64"]
        return users[0]["steamid64"]
    except Exception:  # noqa: BLE001
        return None


def _get_json(url: str, params: dict, timeout: int = 20):
    import requests

    r = requests.get(url, params=params, headers=_HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.json()


def wishlist_appids(steamid64: str) -> list[int]:
    """Keyless wishlist appids via IWishlistService. Empty on any failure / private."""
    try:
        data = _get_json(WISHLIST_URL, {"steamid": steamid64})
        items = (data.get("response") or {}).get("items") or []
        return [int(it["appid"]) for it in items if it.get("appid") is not None]
    except Exception as exc:  # noqa: BLE001
        _log.warning("steam: wishlist fetch failed (%s)", exc)
        return []


def app_details(appid: int, cc: str = "us") -> dict | None:
    """Basic + price data for one app. None on failure / no data."""
    try:
        data = _get_json(APPDETAILS_URL,
                         {"appids": appid, "cc": cc, "filters": "basic,price_overview"})
        entry = data.get(str(appid))
        if not entry or not entry.get("success"):
            return None
        return entry.get("data")
    except Exception as exc:  # noqa: BLE001
        _log.debug("steam: appdetails failed for %s (%s)", appid, exc)
        return None


def _manual_watchlist() -> list[int]:
    from .. import settings

    m = settings.get("steam_watch", [])
    return [int(a) for a in m if str(a).isdigit()] if isinstance(m, list) else []


# ── Manual watchlist management (shared by the CLI + the deck route) ───────────

def parse_appid(value: str) -> "int | None":
    """A Steam appid from a bare number or a store URL (…/app/<id>/…)."""
    import re

    v = str(value).strip()
    if v.isdigit():
        return int(v)
    m = re.search(r"/app/(\d+)", v)
    return int(m.group(1)) if m else None


def get_watchlist() -> list[int]:
    """The manual watchlist (Steam appids)."""
    return _manual_watchlist()


# Serialize the watchlist read-modify-write so two concurrent add/remove calls
# (the route runs them in worker threads) can't lose an update.
_WATCH_LOCK = threading.Lock()


def add_watch(appid: int) -> bool:
    """Add ``appid`` to the manual watchlist. True if added, False if already present."""
    from .. import settings

    with _WATCH_LOCK:
        wl = _manual_watchlist()
        if appid in wl:
            return False
        settings.set("steam_watch", [*wl, appid])
        return True


def remove_watch(appid: int) -> bool:
    """Remove ``appid`` from the manual watchlist. True if removed, False if absent."""
    from .. import settings

    with _WATCH_LOCK:
        wl = _manual_watchlist()
        if appid not in wl:
            return False
        settings.set("steam_watch", [a for a in wl if a != appid])
        return True


def watchlist_named(cc: str = "us", max_workers: int = 10) -> list[dict]:
    """The manual watchlist with resolved names (best-effort via appdetails).

    Resolves the *whole* watchlist — nothing is silently dropped — fetching names
    concurrently (bounded pool) so a longer list doesn't stall the UI. Mirrors
    :func:`check_deals`. A failed lookup falls back to ``App <appid>``.
    """
    appids = _manual_watchlist()
    if not appids:
        return []
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=min(max_workers, len(appids))) as ex:
        details = list(ex.map(lambda a: app_details(a, cc=cc), appids))
    return [
        {
            "appid": appid,
            "name": (data or {}).get("name") or f"App {appid}",
            "url": f"https://store.steampowered.com/app/{appid}/",
        }
        for appid, data in zip(appids, details)
    ]


def _dedupe(ids: list[int]) -> list[int]:
    seen: set[int] = set()
    out: list[int] = []
    for a in ids:
        if a not in seen:
            seen.add(a)
            out.append(a)
    return out


def watched_appids(include_wishlist: bool = True, extra: list[int] | None = None) -> list[int]:
    """Union of the wishlist, the manual watchlist (config) and *extra*."""
    ids: list[int] = []
    if include_wishlist:
        sid = resolve_steamid64()
        if sid:
            ids.extend(wishlist_appids(sid))
    ids.extend(_manual_watchlist())
    if extra:
        ids.extend(int(a) for a in extra)
    return _dedupe(ids)


def _deal_from(appid: int, data: dict | None, threshold: int, wishlist_set: set[int]) -> "SteamDeal | None":
    """Build a SteamDeal from one app's details, or None if it isn't a deal."""
    if not data:
        return None
    po = data.get("price_overview")
    if po is None:
        return None  # unreleased / permanently-free / region-locked — not a deal
    discount = int(po.get("discount_percent") or 0)
    final = int(po.get("final") or 0)
    free_promo = (not bool(data.get("is_free"))) and (final == 0 or discount >= 100)
    if not free_promo and discount < threshold:
        return None
    return SteamDeal(
        appid=appid, name=(data.get("name") or f"App {appid}"),
        discount_pct=discount, final_cents=final, initial_cents=int(po.get("initial") or 0),
        currency=str(po.get("currency") or "USD"),
        final_formatted=str(po.get("final_formatted") or ""),
        is_free=free_promo, on_wishlist=appid in wishlist_set,
        url=APP_URL.format(appid=appid),
    )


# Short-TTL cache so the OS Games "Deals" tab (which re-fetches on every tab
# switch → a daemon route call) is instant on re-open instead of a ~6s round-trip.
# In-memory, so it lives in the long-running daemon — the CLI (fresh process each
# run) is unaffected, and the daily notify path bypasses it for fresh alerts.
_DEALS_TTL = 180.0
_DEALS_CACHE: dict[tuple, tuple[float, dict]] = {}


def _deals_cache_key(cc: str, threshold: int, include_wishlist: bool, extra_appids) -> tuple:
    return (cc, threshold, include_wishlist, tuple(sorted(int(a) for a in (extra_appids or []))))


def clear_deals_cache() -> None:
    """Drop all cached deal results (tests + a future manual refresh)."""
    _DEALS_CACHE.clear()


def check_deals(
    *,
    threshold: int = 20,
    cc: str = "us",
    include_wishlist: bool = True,
    extra_appids: list[int] | None = None,
    cap: int = _MAX_APPS,
    max_workers: int = 10,
    use_cache: bool = True,
    ttl: float = _DEALS_TTL,
) -> dict:
    """Return current deals among watched apps.

    ``{"steamid", "checked", "capped", "deals": [SteamDeal...]}``. A deal is
    free-to-keep or discounted ≥ threshold. Never raises. Resolves the SteamID
    and fetches the wishlist exactly once, then fetches per-app prices
    **concurrently** (bounded pool) — a sequential/paced fetch took ~30s for a
    large wishlist, which is far too slow for a live UI. Results are cached for
    ``ttl`` seconds (``use_cache=False`` to force a fresh fetch).
    """
    key = _deals_cache_key(cc, threshold, include_wishlist, extra_appids)
    if use_cache:
        hit = _DEALS_CACHE.get(key)
        if hit is not None and hit[0] > time.time():
            return hit[1]

    sid = resolve_steamid64()
    wishlist = wishlist_appids(sid) if (include_wishlist and sid) else []
    wishlist_set = set(wishlist)

    appids = _dedupe(wishlist + _manual_watchlist() + [int(a) for a in (extra_appids or [])])
    capped = len(appids) > cap
    appids = appids[:cap]

    deals: list[SteamDeal] = []
    if appids:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(max_workers, len(appids))) as ex:
            details = list(ex.map(lambda a: app_details(a, cc=cc), appids))
        for appid, data in zip(appids, details):
            deal = _deal_from(appid, data, threshold, wishlist_set)
            if deal is not None:
                deals.append(deal)

    deals.sort(key=lambda d: (not d.is_free, -d.discount_pct))
    result = {"steamid": sid, "checked": len(appids), "capped": capped, "deals": deals}
    if use_cache:
        _DEALS_CACHE[key] = (time.time() + ttl, result)
    return result


# ── Steam free-to-keep giveaways ──────────────────────────────────────────────
# Steam's own store search is the authoritative, keyless feed for this: a
# "free to keep" title is a normally-PAID game currently discounted 100%
# (maxprice=free + specials=1). Community "free Steam games" bots/channels are
# just relays of this same endpoint, so we source it directly rather than
# scraping a mirror. The search payload carries no appid — only a capsule image
# whose path embeds it — so we recover the id from the URL and then re-verify
# every candidate through appdetails, which is what keeps a permanently
# free-to-play title (is_free=true, no price_overview) from ever being announced
# as a giveaway.

_FREE_CACHE: dict[tuple, tuple[float, list]] = {}
_FREE_TTL = 300.0


def clear_free_cache() -> None:
    """Drop the free-to-keep cache (tests / --fresh)."""
    _FREE_CACHE.clear()


def _appid_from_capsule(url: str) -> "int | None":
    """Steam's search JSON has no appid — recover it from the capsule image path."""
    import re

    m = re.search(r"/apps/(\d+)/", url or "")
    return int(m.group(1)) if m else None


def _free_to_keep_from(appid: int, data: dict | None) -> "FreeGame | None":
    """A giveaway is a *paid* game at 100% off. Free-to-play titles are refused."""
    if not data or data.get("is_free"):
        return None  # permanently free-to-play — not a giveaway
    po = data.get("price_overview") or {}
    if po.get("final") != 0 or po.get("discount_percent") != 100:
        return None  # not actually free right now
    initial = int(po.get("initial") or 0)
    return FreeGame(
        store="steam",
        title=str(data.get("name") or f"App {appid}"),
        slug=str(appid),
        url=APP_URL.format(appid=appid),
        image=str(data.get("header_image") or ""),
        original_price=str(po.get("initial_formatted") or ""),
        original_price_cents=initial,
        currency=str(po.get("currency") or "USD"),
        is_current=True,
    )


def free_to_keep(
    cc: str = "us",
    lang: str = "english",
    *,
    use_cache: bool = True,
    ttl: float = _FREE_TTL,
    max_workers: int = 10,
) -> "list[FreeGame]":
    """Steam games that are FREE TO KEEP right now (a paid game at 100% off).

    Keyless. Returns :class:`FreeGame` objects (``store="steam"``) so they flow
    through the same Free-games surfaces as Epic. Never raises — a sourcing
    failure yields an empty list rather than breaking the feed.
    """
    key = (cc, lang)
    if use_cache:
        hit = _FREE_CACHE.get(key)
        if hit and hit[0] > time.time():
            return list(hit[1])

    try:
        data = _get_json(SEARCH_URL, {
            "force_infinite": 1, "maxprice": "free", "specials": 1, "l": lang, "json": 1,
        })
        items = (data or {}).get("items") or []
    except Exception as exc:  # noqa: BLE001
        _log.warning("steam: free-to-keep search failed (%s)", exc)
        return []

    appids = _dedupe([a for a in (_appid_from_capsule(i.get("logo")) for i in items) if a])[:_MAX_APPS]
    if not appids:
        _FREE_CACHE[key] = (time.time() + ttl, [])
        return []

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=min(max_workers, len(appids))) as ex:
        details = list(ex.map(lambda a: app_details(a, cc=cc), appids))
    games = [g for g in (_free_to_keep_from(a, d) for a, d in zip(appids, details)) if g]

    _FREE_CACHE[key] = (time.time() + ttl, games)
    _log.info("steam: %d free-to-keep game(s) from %d candidate(s)", len(games), len(appids))
    return list(games)
