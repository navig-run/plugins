"""Item YAML <-> eBay payload mapping and validation.

A listing is authored as a small YAML file (see README). This module loads it,
validates it, and produces the two eBay payloads the Inventory + Offer APIs need:

  * an **inventory item** (the product: title, description, aspects, images,
    condition, quantity), keyed by SKU, and
  * an **offer** (marketplace, price, format, business policies, location).

Keeping this mapping in one tested place means the round-trip invariant
``validate(load(dump(spec)))`` stays stable (CLAUDE.md §7).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import EbayConfig
from .models import EbayConfigError

# eBay ConditionEnum values accepted in an inventory item.
VALID_CONDITIONS = {
    "NEW",
    "LIKE_NEW",
    "NEW_OTHER",
    "NEW_WITH_DEFECTS",
    "CERTIFIED_REFURBISHED",
    "EXCELLENT_REFURBISHED",
    "VERY_GOOD_REFURBISHED",
    "GOOD_REFURBISHED",
    "SELLER_REFURBISHED",
    "USED_EXCELLENT",
    "USED_VERY_GOOD",
    "USED_GOOD",
    "USED_ACCEPTABLE",
    "FOR_PARTS_OR_NOT_WORKING",
}

VALID_FORMATS = {"FIXED_PRICE", "AUCTION"}

TITLE_MAX = 80

# Words that frequently trigger eBay listing restrictions (e.g. Flipper Zero and
# other pentest/RF tools). Not a blocklist — a heads-up so a good account doesn't
# take a policy strike.
_RESTRICTION_TERMS = {
    "hack", "hacking", "hacker", "jammer", "jamming", "rfid clone", "clone card",
    "keyless", "car key", "sub-ghz attack", "deauth", "wifi attack", "skimmer",
    "bypass", "unlock tool",
}


def load_item_file(path: str | Path) -> dict[str, Any]:
    """Load and lightly normalise an item YAML file into a dict spec."""
    p = Path(path)
    if not p.exists():
        raise EbayConfigError(f"item file not found: {p}")
    try:
        import yaml

        with open(p, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as exc:
        raise EbayConfigError(f"could not parse {p}: {exc}") from exc
    if not isinstance(data, dict):
        raise EbayConfigError(f"{p} must contain a YAML mapping")
    # Resolve relative image paths against the file's directory.
    images = data.get("images") or []
    resolved = []
    for img in images:
        s = str(img)
        if s.startswith(("http://", "https://")) or Path(s).is_absolute():
            resolved.append(s)
        else:
            resolved.append(str((p.parent / s).resolve()))
    data["images"] = resolved
    return data


def validate(spec: dict[str, Any]) -> list[str]:
    """Return a list of human-readable validation errors (empty == valid)."""
    errors: list[str] = []

    def require(key: str) -> Any:
        val = spec.get(key)
        if val in (None, "", [], {}):
            errors.append(f"missing required field: {key}")
        return val

    sku = require("sku")
    if sku and not str(sku).strip():
        errors.append("sku must be non-empty")

    title = require("title")
    if title and len(str(title)) > TITLE_MAX:
        errors.append(f"title exceeds {TITLE_MAX} chars ({len(str(title))})")

    require("category_id")
    require("description")

    condition = spec.get("condition", "NEW")
    if condition not in VALID_CONDITIONS:
        errors.append(f"condition {condition!r} is not a valid eBay condition enum")

    fmt = spec.get("format", "FIXED_PRICE")
    if fmt not in VALID_FORMATS:
        errors.append(f"format {fmt!r} must be one of {sorted(VALID_FORMATS)}")

    price = spec.get("price")
    if price is None:
        errors.append("missing required field: price")
    else:
        try:
            if float(price) <= 0:
                errors.append("price must be > 0")
        except (TypeError, ValueError):
            errors.append(f"price {price!r} is not a number")

    qty = spec.get("quantity", 1)
    try:
        if int(qty) < 1:
            errors.append("quantity must be >= 1")
    except (TypeError, ValueError):
        errors.append(f"quantity {qty!r} is not an integer")

    images = spec.get("images") or []
    if not images:
        errors.append("at least one image is required")

    aspects = spec.get("aspects")
    if aspects is not None and not isinstance(aspects, dict):
        errors.append("aspects must be a mapping of name -> list of values")

    return errors


def restriction_warnings(spec: dict[str, Any]) -> list[str]:
    """Flag wording likely to trigger eBay listing restrictions."""
    hay = " ".join(
        str(spec.get(k, "")) for k in ("title", "description")
    ).lower()
    hits = sorted({term for term in _RESTRICTION_TERMS if term in hay})
    if not hits:
        return []
    return [
        "listing wording may trigger an eBay restriction "
        f"(matched: {', '.join(hits)}); consider neutral, hobby/dev framing"
    ]


def _normalise_aspects(aspects: Any) -> dict[str, list[str]]:
    """eBay aspects are name -> list[str]. Coerce scalars into single-item lists."""
    out: dict[str, list[str]] = {}
    if isinstance(aspects, dict):
        for name, value in aspects.items():
            if isinstance(value, list):
                out[str(name)] = [str(v) for v in value]
            else:
                out[str(name)] = [str(value)]
    return out


def to_inventory_item_payload(spec: dict[str, Any], *, image_urls: list[str] | None = None) -> dict[str, Any]:
    """Build the PUT body for the Inventory API inventory_item endpoint."""
    product: dict[str, Any] = {
        "title": str(spec["title"]),
        "description": str(spec["description"]),
    }
    aspects = _normalise_aspects(spec.get("aspects"))
    if aspects:
        product["aspects"] = aspects
    urls = image_urls if image_urls is not None else list(spec.get("images") or [])
    if urls:
        product["imageUrls"] = urls

    return {
        "product": product,
        "condition": spec.get("condition", "NEW"),
        "availability": {
            "shipToLocationAvailability": {"quantity": int(spec.get("quantity", 1))}
        },
    }


def to_offer_payload(spec: dict[str, Any], config: EbayConfig) -> dict[str, Any]:
    """Build the POST body for the Offer API create-offer endpoint.

    Business-policy and location defaults fall back to the config when the item
    file omits them.
    """
    policy = spec.get("policy") or {}
    payment = policy.get("payment") or config.default_payment_policy_id
    ret = policy.get("return") or config.default_return_policy_id
    fulfillment = policy.get("fulfillment") or config.default_fulfillment_policy_id
    location_key = spec.get("location_key") or config.default_location_key

    listing_policies: dict[str, Any] = {}
    if payment:
        listing_policies["paymentPolicyId"] = str(payment)
    if ret:
        listing_policies["returnPolicyId"] = str(ret)
    if fulfillment:
        listing_policies["fulfillmentPolicyId"] = str(fulfillment)
    if spec.get("best_offer"):
        listing_policies["bestOfferTerms"] = {"bestOfferEnabled": True}

    offer: dict[str, Any] = {
        "sku": str(spec["sku"]),
        "marketplaceId": config.marketplace_id,
        "format": spec.get("format", "FIXED_PRICE"),
        "availableQuantity": int(spec.get("quantity", 1)),
        "categoryId": str(spec["category_id"]),
        "listingDescription": str(spec["description"]),
        "pricingSummary": {
            "price": {
                "value": f"{float(spec['price']):.2f}",
                "currency": spec.get("currency", "USD"),
            }
        },
    }
    if listing_policies:
        offer["listingPolicies"] = listing_policies
    if location_key:
        offer["merchantLocationKey"] = str(location_key)
    return offer
