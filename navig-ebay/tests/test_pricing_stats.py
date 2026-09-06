"""Browse-API price-stat maths (pure, no network)."""

from __future__ import annotations

from navig_ebay.engine import pricing


def _item(value, currency="USD"):
    return {"price": {"value": value, "currency": currency}}


def test_empty_returns_zero_count():
    stats = pricing.price_stats([])
    assert stats["count"] == 0
    assert stats["low"] is None and stats["median"] is None and stats["high"] is None


def test_low_median_high():
    items = [_item("100"), _item("200"), _item("300")]
    stats = pricing.price_stats(items)
    assert stats["count"] == 3
    assert stats["low"] == 100.0
    assert stats["median"] == 200.0
    assert stats["high"] == 300.0
    assert stats["currency"] == "USD"
    assert "NOT sold" in stats["note"]


def test_even_count_median_is_average_of_middle():
    items = [_item("10"), _item("20"), _item("30"), _item("40")]
    assert pricing.price_stats(items)["median"] == 25.0


def test_ignores_unpriced_and_nonpositive_and_garbage():
    items = [
        _item("50"),
        {"price": {"currency": "USD"}},  # no value
        _item("0"),  # non-positive
        _item("abc"),  # garbage
        {},  # no price
        _item("150"),
    ]
    stats = pricing.price_stats(items)
    assert stats["count"] == 2
    assert stats["low"] == 50.0
    assert stats["high"] == 150.0
