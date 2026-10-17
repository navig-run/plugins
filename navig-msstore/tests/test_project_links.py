"""Projects hub link card for a Microsoft Store app (local sales history only)."""

from __future__ import annotations

import datetime as dt
import json

import pytest


@pytest.fixture
def hist(monkeypatch):
    from navig_msstore import project_links

    rows: dict = {}
    monkeypatch.setattr(project_links, "_history_cache", None)
    monkeypatch.setattr("navig_msstore.sales.load_history", lambda path=None: {"rows": rows})
    return rows


def test_no_identity_no_card(tmp_path, hist):
    from navig_msstore.project_links import store_card

    assert store_card(str(tmp_path), "x") is None
    (tmp_path / "store").mkdir()
    (tmp_path / "store" / "identity.json").write_text("{}", encoding="utf-8")
    assert store_card(str(tmp_path), "x") is None  # no product id → not linked


def test_card_sums_only_this_app_and_the_last_30_days(tmp_path, hist):
    from navig_msstore.project_links import store_card

    (tmp_path / "store").mkdir()
    (tmp_path / "store" / "identity.json").write_text(json.dumps({"storeProductId": "9NAPP"}), encoding="utf-8")
    today = dt.date.today()
    hist["a"] = {"app_id": "9NAPP", "app_name": "Snapthos", "date": today.isoformat(), "quantity": 3, "usd": 6.0}
    hist["b"] = {"app_id": "9NAPP", "date": (today - dt.timedelta(days=60)).isoformat(), "quantity": 9, "usd": 18.0}
    hist["c"] = {"app_id": "9NOTHER", "date": today.isoformat(), "quantity": 50, "usd": 100.0}
    card = store_card(str(tmp_path), "x")
    metrics = {m["label"]: m["value"] for m in card["metrics"]}
    assert card["title"] == "Microsoft Store · Snapthos" and card["match"] == "store"
    assert metrics == {"Sales (30d)": 3, "Revenue (30d)": 6.0, "Revenue (tracked)": 24.0}
