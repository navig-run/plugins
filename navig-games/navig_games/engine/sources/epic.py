"""Epic Games Store sourcing.

Primary source is Epic's public, auth-free promotions endpoint — the same data
that powers the store's "Free Now" row — so we never scrape HTML:

    https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions

We parse ``data.Catalog.searchStore.elements`` and keep an element that has an
active ``promotions.promotionalOffers`` window AND ``price.totalPrice.discountPrice
== 0``. Upcoming freebies come from ``upcomingPromotionalOffers``. A best-effort
fallback reads a community-maintained JSON list if the primary endpoint fails.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from ..models import FreeGame

_log = logging.getLogger(__name__)

PROMOTIONS_URL = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
STORE_BASE = "https://store.epicgames.com"
FREE_GAMES_PAGE = STORE_BASE + "/{locale}/free-games"
# Community fallback (historical list, best-effort current detection).
FALLBACK_JSON = "https://josephmate.github.io/EpicFreeGamesList/epic_free_games.json"

_IMAGE_PREFERENCE = (
    "OfferImageWide",
    "DieselStoreFrontWide",
    "DieselGameBoxWide",
    "Thumbnail",
    "OfferImageTall",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        # Epic uses e.g. "2024-01-04T16:00:00.000Z"
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _http_get_json(url: str, params: dict | None = None, timeout: int = 20) -> Any:
    import requests

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
    }
    resp = requests.get(url, params=params, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _pick_image(el: dict) -> str:
    images = el.get("keyImages") or []
    by_type = {img.get("type"): img.get("url") for img in images if img.get("url")}
    for pref in _IMAGE_PREFERENCE:
        if by_type.get(pref):
            return by_type[pref]
    return next(iter(by_type.values()), "") if by_type else ""


def _build_url(el: dict, locale: str) -> str:
    """Best-effort product URL for display/notify. The claim engine independently
    resolves claimable URLs from the live free-games page, so a fuzzy guess here
    is harmless."""
    candidates: list[tuple[str | None, str]] = []
    for mapping in el.get("offerMappings") or []:
        if mapping.get("pageSlug"):
            candidates.append((mapping.get("pageType"), mapping["pageSlug"]))
    catalog_ns = el.get("catalogNs") or {}
    for mapping in catalog_ns.get("mappings") or []:
        if mapping.get("pageSlug"):
            candidates.append((mapping.get("pageType"), mapping["pageSlug"]))
    if el.get("productSlug"):
        candidates.append((None, el["productSlug"]))
    if el.get("urlSlug"):
        candidates.append((None, el["urlSlug"]))

    if not candidates:
        return FREE_GAMES_PAGE.format(locale=locale)

    page_type, slug = candidates[0]
    slug = str(slug).split("/")[0]  # productSlug sometimes trails "/home"
    prefix = "p"
    if (el.get("offerType") == "BUNDLE") or (page_type == "productBundle"):
        prefix = "bundles"
    return f"{STORE_BASE}/{locale}/{prefix}/{slug}"


def _active_offer(groups: list[dict], now: datetime) -> dict | None:
    """Return the first promo offer whose [start, end) window contains *now*."""
    for group in groups or []:
        for offer in group.get("promotionalOffers") or []:
            start = _parse_dt(offer.get("startDate"))
            end = _parse_dt(offer.get("endDate"))
            if start and end and start <= now < end:
                return offer
    return None


def _first_upcoming(groups: list[dict], now: datetime) -> dict | None:
    best: dict | None = None
    best_start: datetime | None = None
    for group in groups or []:
        for offer in group.get("promotionalOffers") or []:
            start = _parse_dt(offer.get("startDate"))
            if start and start > now:
                if best_start is None or start < best_start:
                    best_start, best = start, offer
    return best


def _element_to_game(el: dict, locale: str, *, current: bool, offer: dict) -> FreeGame:
    price = (el.get("price") or {}).get("totalPrice") or {}
    fmt = price.get("fmtPrice") or {}
    return FreeGame(
        store="epic",
        title=el.get("title") or "(untitled)",
        offer_id=str(el.get("id") or ""),
        namespace=str(el.get("namespace") or ""),
        slug=str(el.get("productSlug") or el.get("urlSlug") or ""),
        url=_build_url(el, locale),
        description=(el.get("description") or "").strip(),
        seller=((el.get("seller") or {}).get("name") or "").strip(),
        image=_pick_image(el),
        original_price=str(fmt.get("originalPrice") or ""),
        original_price_cents=int(price.get("originalPrice") or 0),
        currency=str(price.get("currencyCode") or "USD"),
        starts_at=str(offer.get("startDate") or ""),
        ends_at=str(offer.get("endDate") or ""),
        is_current=current,
    )


def fetch_free_games(
    country: str = "US", locale: str = "en-US", timeout: int = 20
) -> dict[str, list[FreeGame]]:
    """Return ``{"current": [...], "upcoming": [...]}`` for Epic.

    Never raises — on total failure returns empty lists (callers degrade
    gracefully). A game is *current* only when the promo window is active AND
    the discounted price is exactly 0.
    """
    now = _now()
    current: list[FreeGame] = []
    upcoming: list[FreeGame] = []
    try:
        payload = _http_get_json(
            PROMOTIONS_URL,
            params={"locale": locale, "country": country, "allowCountries": country},
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 — degrade to fallback
        _log.warning("epic: primary promotions endpoint failed (%s); trying fallback", exc)
        return _fallback_free_games(country, locale, timeout)

    try:
        elements = (
            payload.get("data", {})
            .get("Catalog", {})
            .get("searchStore", {})
            .get("elements", [])
        )
    except AttributeError:
        elements = []

    for el in elements:
        promos = el.get("promotions") or {}
        if not promos:
            continue
        price = (el.get("price") or {}).get("totalPrice") or {}
        discount_cents = price.get("discountPrice")

        active = _active_offer(promos.get("promotionalOffers") or [], now)
        if active is not None and discount_cents == 0:
            current.append(_element_to_game(el, locale, current=True, offer=active))
            continue

        upcoming_offer = _first_upcoming(promos.get("upcomingPromotionalOffers") or [], now)
        if upcoming_offer is not None:
            upcoming.append(_element_to_game(el, locale, current=False, offer=upcoming_offer))

    # De-dupe by key (Epic occasionally lists an offer under two mappings).
    current = _dedupe(current)
    upcoming = _dedupe(upcoming)
    return {"current": current, "upcoming": upcoming}


def _dedupe(games: list[FreeGame]) -> list[FreeGame]:
    seen: set[str] = set()
    out: list[FreeGame] = []
    for g in games:
        if g.key in seen:
            continue
        seen.add(g.key)
        out.append(g)
    return out


def _fallback_free_games(country: str, locale: str, timeout: int) -> dict[str, list[FreeGame]]:
    """Community JSON fallback — best-effort, current games only."""
    try:
        data = _http_get_json(FALLBACK_JSON, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        _log.warning("epic: fallback source also failed (%s)", exc)
        return {"current": [], "upcoming": []}

    now = _now()
    current: list[FreeGame] = []
    entries = data if isinstance(data, list) else data.get("games", [])
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        start = _parse_dt(entry.get("startDate") or entry.get("start"))
        end = _parse_dt(entry.get("endDate") or entry.get("end"))
        if start and end and not (start <= now < end):
            continue
        title = entry.get("title") or entry.get("name")
        if not title:
            continue
        current.append(
            FreeGame(
                store="epic",
                title=title,
                slug=str(entry.get("slug") or entry.get("urlSlug") or ""),
                url=str(entry.get("url") or FREE_GAMES_PAGE.format(locale=locale)),
                starts_at=str(entry.get("startDate") or ""),
                ends_at=str(entry.get("endDate") or ""),
                is_current=True,
            )
        )
    return {"current": _dedupe(current), "upcoming": []}


def current_free(country: str = "US", locale: str = "en-US") -> list[FreeGame]:
    """Convenience: just the currently-claimable free games."""
    return fetch_free_games(country=country, locale=locale)["current"]
