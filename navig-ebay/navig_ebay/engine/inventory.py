"""Inventory API — inventory items (products keyed by SKU).

Docs: /sell/inventory/v1/inventory_item
"""

from __future__ import annotations

from typing import Any

from .api import EbayClient

_BASE = "/sell/inventory/v1/inventory_item"


def create_or_replace(client: EbayClient, sku: str, payload: dict[str, Any]) -> None:
    """PUT an inventory item (create or full replace). Returns 200/204 with no body."""
    client.request(
        "PUT",
        f"{_BASE}/{sku}",
        json=payload,
        # Inventory item PUT wants the content language header explicitly.
        headers={"Content-Language": client.config.content_language},
        allow=(200, 201, 204),
    )


def get(client: EbayClient, sku: str) -> dict[str, Any]:
    return client.get(f"{_BASE}/{sku}")


def list_items(client: EbayClient, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
    return client.get(_BASE, params={"limit": limit, "offset": offset})


def delete(client: EbayClient, sku: str) -> None:
    client.delete(f"{_BASE}/{sku}")
