"""Item YAML validation + payload mapping (round-trip regression, CLAUDE.md §7)."""

from __future__ import annotations

import textwrap

import pytest

from navig_ebay.engine import item_schema
from navig_ebay.engine.config import EbayConfig
from navig_ebay.engine.models import EbayConfigError


def _good_spec() -> dict:
    return {
        "sku": "flipper-01",
        "title": "Flipper Zero Portable Multi-Tool Development Board",
        "condition": "NEW",
        "category_id": "175673",
        "description": "<p>Boxed. Tested working.</p>",
        "aspects": {"Brand": ["Flipper Devices"], "Type": "Development Board"},
        "images": ["https://example.com/1.jpg"],
        "price": 149.0,
        "currency": "USD",
        "quantity": 1,
        "format": "FIXED_PRICE",
        "best_offer": True,
    }


def test_valid_spec_has_no_errors():
    assert item_schema.validate(_good_spec()) == []


def test_missing_fields_reported():
    errors = item_schema.validate({"sku": "x"})
    joined = " ".join(errors)
    assert "title" in joined
    assert "category_id" in joined
    assert "price" in joined
    assert "image" in joined


def test_title_too_long():
    spec = _good_spec()
    spec["title"] = "x" * 81
    assert any("exceeds" in e for e in item_schema.validate(spec))


def test_bad_condition_and_format_and_price():
    spec = _good_spec()
    spec["condition"] = "MINT"
    spec["format"] = "DUTCH"
    spec["price"] = -5
    errors = item_schema.validate(spec)
    assert any("condition" in e for e in errors)
    assert any("format" in e for e in errors)
    assert any("price" in e for e in errors)


def test_inventory_payload_shape():
    payload = item_schema.to_inventory_item_payload(_good_spec())
    assert payload["condition"] == "NEW"
    assert payload["product"]["title"].startswith("Flipper")
    # scalar aspect is coerced to a single-item list
    assert payload["product"]["aspects"]["Type"] == ["Development Board"]
    assert payload["availability"]["shipToLocationAvailability"]["quantity"] == 1
    assert payload["product"]["imageUrls"] == ["https://example.com/1.jpg"]


def test_offer_payload_uses_config_defaults():
    cfg = EbayConfig(
        marketplace_id="EBAY_US",
        default_payment_policy_id="PAY1",
        default_return_policy_id="RET1",
        default_fulfillment_policy_id="FUL1",
        default_location_key="home-1",
    )
    offer = item_schema.to_offer_payload(_good_spec(), cfg)
    assert offer["sku"] == "flipper-01"
    assert offer["categoryId"] == "175673"
    assert offer["pricingSummary"]["price"] == {"value": "149.00", "currency": "USD"}
    lp = offer["listingPolicies"]
    assert lp["paymentPolicyId"] == "PAY1"
    assert lp["returnPolicyId"] == "RET1"
    assert lp["fulfillmentPolicyId"] == "FUL1"
    assert lp["bestOfferTerms"] == {"bestOfferEnabled": True}
    assert offer["merchantLocationKey"] == "home-1"


def test_item_file_roundtrip_and_relative_image_resolution(tmp_path):
    f = tmp_path / "item.yaml"
    f.write_text(
        textwrap.dedent(
            """
            sku: cpu-01
            title: Ryzen 5 5600X CPU Tested Working
            condition: USED_EXCELLENT
            category_id: "164"
            description: Tested working, no bent pins.
            images:
              - ./photos/cpu.jpg
              - https://cdn.example.com/cpu2.jpg
            price: 95
            quantity: 1
            """
        ).strip(),
        encoding="utf-8",
    )
    spec = item_schema.load_item_file(f)
    assert spec["sku"] == "cpu-01"
    # local path resolved to absolute against the file dir; URL untouched
    assert spec["images"][0].endswith("cpu.jpg")
    assert "photos" in spec["images"][0].replace("\\", "/")
    assert spec["images"][1] == "https://cdn.example.com/cpu2.jpg"
    assert item_schema.validate(spec) == []


def test_missing_file_raises():
    with pytest.raises(EbayConfigError):
        item_schema.load_item_file("/no/such/item.yaml")


def test_restriction_warning_for_hacking_language():
    spec = _good_spec()
    spec["description"] = "Great for RFID clone and car key hacking"
    warnings = item_schema.restriction_warnings(spec)
    assert warnings and "restriction" in warnings[0]


def test_no_restriction_warning_for_neutral_language():
    assert item_schema.restriction_warnings(_good_spec()) == []
