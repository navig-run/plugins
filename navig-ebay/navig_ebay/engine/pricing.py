"""Browse API price proxy.

IMPORTANT: this reports **active asking prices**, not sold prices. eBay's
sold-comps data (Marketplace Insights API) is approval-gated and rarely granted
to individuals; the freely-available Browse API only exposes live listings. All
output must be labelled accordingly so a user never mistakes asking for sold.

The Browse API uses a client-credentials **application** token (not the user
token), so callers pass an :class:`EbayClient` constructed with ``token_kind="app"``.
"""

from __future__ import annotations

from statistics import median
from typing import Any

from .api import EbayClient

_SEARCH = "/buy/browse/v1/item_summary/search"


def search_active(client: EbayClient, query: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """Return active item summaries matching *query* (up to *limit*)."""
    resp = client.get(_SEARCH, params={"q": query, "limit": max(1, min(limit, 200))})
    return list(resp.get("itemSummaries", []))


def _extract_price(item: dict[str, Any]) -> float | None:
    price = item.get("price") or {}
    value = price.get("value")
    if value is None:
        return None
    try:
        val = float(value)
    except (TypeError, ValueError):
        return None
    return val if val > 0 else None


def price_stats(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute low/median/high over the active listings' prices.

    Pure function (no network) so the maths is unit-tested. Returns a dict with
    count, currency, and the three stats (None-safe when no priced items).
    """
    prices: list[float] = []
    currency = None
    for item in items:
        val = _extract_price(item)
        if val is None:
            continue
        prices.append(val)
        if currency is None:
            currency = (item.get("price") or {}).get("currency")

    if not prices:
        return {"count": 0, "currency": currency, "low": None, "median": None, "high": None}

    prices.sort()
    return {
        "count": len(prices),
        "currency": currency or "USD",
        "low": round(prices[0], 2),
        "median": round(float(median(prices)), 2),
        "high": round(prices[-1], 2),
        "note": "ACTIVE asking prices — NOT sold prices",
    }
