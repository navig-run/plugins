"""Negotiation API — best-offer eligibility and seller-initiated offers.

Docs: /sell/negotiation/v1
v1 exposes the items eligible for a seller-initiated offer to interested buyers.
"""

from __future__ import annotations

from typing import Any

from .api import EbayClient

_BASE = "/sell/negotiation/v1"


def find_eligible_items(client: EbayClient, *, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
    """Listings eligible for a seller-initiated 'Offer to buyers'."""
    resp = client.get(
        f"{_BASE}/find_eligible_items", params={"limit": max(1, min(limit, 200)), "offset": offset}
    )
    return list(resp.get("eligibleItems", []))
