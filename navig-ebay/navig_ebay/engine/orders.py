"""Fulfillment API — orders and shipping fulfillment.

Docs: /sell/fulfillment/v1/order
"""

from __future__ import annotations

from typing import Any

from .api import EbayClient

_BASE = "/sell/fulfillment/v1/order"


def list_orders(client: EbayClient, *, limit: int = 50, filter_: str | None = None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"limit": max(1, min(limit, 200))}
    if filter_:
        params["filter"] = filter_
    resp = client.get(_BASE, params=params)
    return list(resp.get("orders", []))


def get_order(client: EbayClient, order_id: str) -> dict[str, Any]:
    return client.get(f"{_BASE}/{order_id}")


def create_shipping_fulfillment(
    client: EbayClient,
    order_id: str,
    *,
    tracking_number: str,
    carrier: str,
    line_items: list[dict[str, Any]] | None = None,
) -> str:
    """Mark an order shipped with tracking. Returns the fulfillment id (from Location header if present)."""
    payload: dict[str, Any] = {
        "trackingNumber": tracking_number,
        "shippingCarrierCode": carrier,
    }
    if line_items:
        payload["lineItems"] = line_items
    resp = client.request(
        "POST", f"{_BASE}/{order_id}/shipping_fulfillment", json=payload, allow=(200, 201)
    )
    return str(resp.get("fulfillmentId", "")) if isinstance(resp, dict) else ""
