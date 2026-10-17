"""Location API — merchant inventory locations (a publish prerequisite).

Docs: /sell/inventory/v1/location
"""

from __future__ import annotations

from typing import Any

from .api import EbayClient
from .models import EbayApiError

_BASE = "/sell/inventory/v1/location"


def create(
    client: EbayClient,
    key: str,
    *,
    country: str = "US",
    postal_code: str | None = None,
    city: str | None = None,
    state: str | None = None,
    address_line1: str | None = None,
    name: str | None = None,
) -> None:
    """Create (or ensure) a merchant location. eBay returns 204 on success."""
    address: dict[str, Any] = {"country": country}
    if postal_code:
        address["postalCode"] = postal_code
    if city:
        address["city"] = city
    if state:
        address["stateOrProvince"] = state
    if address_line1:
        address["addressLine1"] = address_line1

    payload = {
        "location": {"address": address},
        "name": name or key,
        "merchantLocationStatus": "ENABLED",
        "locationTypes": ["WAREHOUSE"],
    }
    client.request("POST", f"{_BASE}/{key}", json=payload, allow=(200, 201, 204))


def list_locations(client: EbayClient) -> list[dict[str, Any]]:
    resp = client.get(_BASE)
    return list(resp.get("locations", []))


def exists(client: EbayClient, key: str) -> bool:
    try:
        client.get(f"{_BASE}/{key}")
        return True
    except EbayApiError as exc:
        if exc.status == 404:
            return False
        raise
