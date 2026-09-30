"""Account API — business policies (payment / return / fulfillment).

An offer cannot be published without these, and the account must be opted in to
business policies. Docs: /sell/account/v1/{payment,return,fulfillment}_policy
"""

from __future__ import annotations

from typing import Any

from .api import EbayClient
from .models import EbayApiError

_KINDS = {
    "payment": ("payment_policy", "paymentPolicies", "paymentPolicyId"),
    "return": ("return_policy", "returnPolicies", "returnPolicyId"),
    "fulfillment": ("fulfillment_policy", "fulfillmentPolicies", "fulfillmentPolicyId"),
}


def fetch(client: EbayClient, kind: str) -> list[dict[str, Any]]:
    """Return the account's policies of *kind* for the active marketplace."""
    if kind not in _KINDS:
        raise ValueError(f"unknown policy kind {kind!r}; expected {sorted(_KINDS)}")
    path_seg, list_key, _id_key = _KINDS[kind]
    try:
        resp = client.get(
            f"/sell/account/v1/{path_seg}",
            params={"marketplace_id": client.config.marketplace_id},
        )
    except EbayApiError as exc:
        # 404 = none defined yet; surface a clean empty list.
        if exc.status == 404:
            return []
        raise
    return list(resp.get(list_key, []))


def fetch_all(client: EbayClient) -> dict[str, list[dict[str, Any]]]:
    """All three policy kinds keyed by kind."""
    return {kind: fetch(client, kind) for kind in _KINDS}


def id_key(kind: str) -> str:
    return _KINDS[kind][2]
