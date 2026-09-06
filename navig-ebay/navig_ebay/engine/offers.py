"""Offer API — offers turn an inventory item into a (publishable) listing.

Docs: /sell/inventory/v1/offer
"""

from __future__ import annotations

from typing import Any

from .api import EbayClient
from .models import EbayApiError

_BASE = "/sell/inventory/v1/offer"


def create(client: EbayClient, payload: dict[str, Any]) -> str:
    """Create an unpublished offer; return its offerId."""
    resp = client.request("POST", _BASE, json=payload, allow=(200, 201))
    return str(resp.get("offerId", ""))


def get_for_sku(client: EbayClient, sku: str) -> list[dict[str, Any]]:
    """Return existing offers for a SKU (empty list if none)."""
    try:
        resp = client.get(_BASE, params={"sku": sku})
    except EbayApiError as exc:
        # eBay returns 404 when a SKU has no offers yet.
        if exc.status == 404:
            return []
        raise
    return list(resp.get("offers", []))


def update(client: EbayClient, offer_id: str, payload: dict[str, Any]) -> None:
    client.request("PUT", f"{_BASE}/{offer_id}", json=payload, allow=(200, 204))


def publish(client: EbayClient, offer_id: str) -> str:
    """Publish an offer → live listing. Returns the listingId."""
    resp = client.request("POST", f"{_BASE}/{offer_id}/publish", allow=(200,))
    return str(resp.get("listingId", ""))


def withdraw(client: EbayClient, offer_id: str) -> str:
    """End a published listing (withdraw the offer). Returns the listingId ended."""
    resp = client.request("POST", f"{_BASE}/{offer_id}/withdraw", allow=(200,))
    return str(resp.get("listingId", ""))


def delete(client: EbayClient, offer_id: str) -> None:
    client.delete(f"{_BASE}/{offer_id}")


def get(client: EbayClient, offer_id: str) -> dict[str, Any]:
    return client.get(f"{_BASE}/{offer_id}")
