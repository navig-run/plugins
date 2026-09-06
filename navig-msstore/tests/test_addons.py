"""Add-on helpers: the DevCenter API returns the same concept under two different
shapes, and clone-then-override must never drop a field it did not write.

Every case here is anchored to a real response observed against the live Ingestion
API while creating the `blindspot-pro` add-on (app 9N8LFHMRKS92) -- the two
id spellings in particular are what made `addon create` first print `None`.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from navig_msstore.addons import (
    _creds_from_file,
    _seed_required,
    addon_app_ids,
    addon_store_id,
    apply_listing,
    apply_pricing,
    build_sale,
    strip_readonly,
)

# ── the two id spellings ───────────────────────────────────────────────────────


def test_store_id_from_get_shape():
    """GET /inappproducts/{id} returns the Store ID as `id`."""
    assert addon_store_id({"id": "9NJ1MXXMS137", "productId": "blindspot-pro"}) == "9NJ1MXXMS137"


def test_store_id_from_list_shape():
    """listinappproducts returns it as `inAppProductId`."""
    assert addon_store_id({"inAppProductId": "9NJ1MXXMS137"}) == "9NJ1MXXMS137"


def test_store_id_missing_is_none():
    assert addon_store_id({"productId": "blindspot-pro"}) is None


def test_app_ids_from_paged_get_shape():
    """GET returns a paged `applications` object, not the flat list create accepts."""
    product = {
        "applications": {
            "value": [{"id": "9N8LFHMRKS92", "resourceLocation": "applications/9N8LFHMRKS92"}],
            "totalCount": 1,
        }
    }
    assert addon_app_ids(product) == ["9N8LFHMRKS92"]


def test_app_ids_from_flat_create_shape():
    assert addon_app_ids({"applicationIds": ["9N8LFHMRKS92"]}) == ["9N8LFHMRKS92"]


def test_app_ids_absent_is_empty():
    assert addon_app_ids({}) == []


# ── read-only stripping ────────────────────────────────────────────────────────


def test_strip_readonly_removes_server_owned_fields_only():
    draft = {
        "id": "115292",
        "status": "PendingCommit",
        "statusDetails": {"errors": []},
        "fileUploadUrl": "https://blob...",
        "contentType": "NotSet",
        "listings": {},
    }
    out = strip_readonly(draft)
    assert set(out) == {"contentType", "listings"}
    assert "id" in draft, "input must not be mutated"


# ── clone-then-override ────────────────────────────────────────────────────────


def test_apply_pricing_preserves_unknown_sibling_fields():
    """The whole point of clone-then-override: fields we never heard of survive."""
    body = {"pricing": {"isAdvancedPricingModel": True, "someFutureField": 7}}
    out = apply_pricing(body, base_tier="Tier1252")
    assert out["pricing"]["priceId"] == "Tier1252"
    assert out["pricing"]["isAdvancedPricingModel"] is True
    assert out["pricing"]["someFutureField"] == 7
    assert out["pricing"]["sales"] == []


def test_apply_pricing_does_not_mutate_input():
    body = {"pricing": {"priceId": "Free"}}
    apply_pricing(body, base_tier="Tier1252")
    assert body["pricing"]["priceId"] == "Free"


def test_apply_pricing_attaches_sale():
    start = datetime(2026, 8, 13, tzinfo=timezone.utc)
    sale = build_sale("Launch", "Tier1152", start, start + timedelta(days=90))
    out = apply_pricing({}, base_tier="Tier1252", sale=sale)
    assert out["pricing"]["sales"] == [sale]


def test_apply_listing_preserves_other_languages():
    body = {"listings": {"de-de": {"title": "Alt"}}}
    out = apply_listing(body, lang="en-us", title="Blindspot Pro", description="d")
    assert out["listings"]["de-de"]["title"] == "Alt"
    assert out["listings"]["en-us"] == {"title": "Blindspot Pro", "description": "d"}


# ── sales ──────────────────────────────────────────────────────────────────────


def test_build_sale_formats_utc_and_orders_dates():
    start = datetime(2026, 8, 13, 12, 30, 0, tzinfo=timezone.utc)
    sale = build_sale("Launch", "Tier1152", start, start + timedelta(days=90))
    assert sale["startDate"] == "2026-08-13T12:30:00Z"
    assert sale["endDate"] == "2026-11-11T12:30:00Z"
    assert sale["basePriceId"] == "Tier1152"


def test_build_sale_rejects_non_positive_window():
    """An open-ended or inverted 'sale' is just a price wearing a discount badge."""
    start = datetime(2026, 8, 13, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        build_sale("Bad", "Tier1152", start, start)


# ── draft seeding ──────────────────────────────────────────────────────────────


def test_seed_required_replaces_notset_content_type():
    """A fresh draft comes back with contentType 'NotSet', which the API rejects."""
    out = _seed_required({"contentType": "NotSet"}, title="t", description="d")
    assert out["contentType"] != "NotSet"
    assert out["lifetime"] == "Forever"
    assert out["visibility"] == "Public"
    assert out["targetPublishMode"] == "Manual"
    assert out["listings"]["en-us"]["title"] == "t"


def test_seed_required_keeps_an_existing_content_type():
    out = _seed_required({"contentType": "OnlineDataStorage"}, title="t", description="d")
    assert out["contentType"] == "OnlineDataStorage"


# ── creds file ─────────────────────────────────────────────────────────────────


def test_creds_file_accepts_both_spellings(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"tenant_id": "t", "client_id": "c", "client_secret": "s"}))
    assert _creds_from_file(p) == ("t", "c", "s")


def test_creds_file_incomplete_is_none(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"tenantId": "t", "clientId": "c"}))
    assert _creds_from_file(p) is None


def test_creds_file_unparseable_is_none(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("not json")
    assert _creds_from_file(p) is None


# ── dashboard-owned submissions (quirk #5) ─────────────────────────────────────


def test_dashboard_owned_is_detected_and_explained():
    """The raw API text says "delete the submission" -- which would destroy whatever a
    human has open in Partner Center. The explanation must surface that choice."""
    from navig_msstore.addons import DevCenterError, explain, is_dashboard_owned

    exc = DevCenterError(
        400,
        json.dumps(
            {
                "code": "InvalidOperation",
                "message": (
                    "Ingestion API can only update, delete, and commit submissions "
                    "that are created through the API. Please delete the current "
                    "in-progress submission and create a new one."
                ),
            }
        ),
        "https://example/submissions/1",
    )
    assert is_dashboard_owned(exc)
    msg = explain(exc)
    assert "dashboard" in msg
    assert "submission-delete" in msg, "must name the escape hatch"
    assert "confirm first" in msg, "must warn before destroying open work"


def test_explain_falls_back_to_api_message():
    from navig_msstore.addons import DevCenterError, explain, is_dashboard_owned

    exc = DevCenterError(400, json.dumps({"message": "The length of Title must be 100 or less"}), "u")
    assert not is_dashboard_owned(exc)
    assert explain(exc) == "The length of Title must be 100 or less"


def test_explain_survives_a_non_json_body():
    from navig_msstore.addons import DevCenterError, explain

    assert "502" in explain(DevCenterError(502, "<html>bad gateway</html>", "u"))


# ── zombie submissions: PUT returns 200 but stores nothing ─────────────────────


def test_readback_drift_flags_a_zombie_submission():
    """A submission born from a failed (500) create accepts PUTs and persists none of
    it. Committing that would ship an empty listing at the wrong price."""
    from navig_msstore.addons import _readback_drift

    sent = {
        "contentType": "OnlineDataStorage",
        "pricing": {"priceId": "Tier1152"},
        "listings": {"en-us": {"title": "Blindspot Pro"}},
    }
    stored = {"contentType": "NotSet", "pricing": {"priceId": "Base"}, "listings": {}}
    drift = _readback_drift(stored, sent)
    assert len(drift) == 3
    assert any("contentType" in d for d in drift)
    assert any("priceId" in d for d in drift)
    assert any("listings" in d for d in drift)


def test_readback_drift_silent_on_a_healthy_write():
    from navig_msstore.addons import _readback_drift

    body = {
        "contentType": "OnlineDataStorage",
        "pricing": {"priceId": "Tier1152"},
        "listings": {"en-us": {"title": "Blindspot Pro"}},
    }
    assert _readback_drift(json.loads(json.dumps(body)), body) == []


def test_seed_required_uses_an_accepted_content_type():
    """`NotDownloadableContent` -- what the docs suggest -- is rejected by the API."""
    from navig_msstore.addons import CONTENT_TYPES, _seed_required

    out = _seed_required({"contentType": "NotSet"}, title="t", description="d")
    assert out["contentType"] in CONTENT_TYPES
    assert out["contentType"] != "NotDownloadableContent"
