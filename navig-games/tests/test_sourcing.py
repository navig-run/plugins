"""Epic sourcing parser — current vs upcoming, and the $0 filter."""

from datetime import datetime, timedelta, timezone

from navig_games.engine.sources import epic


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _payload():
    now = datetime.now(timezone.utc)
    past = _iso(now - timedelta(days=1))
    soon = _iso(now + timedelta(days=5))
    later = _iso(now + timedelta(days=12))

    def price(discount, original, fmt_original):
        return {"totalPrice": {
            "discountPrice": discount, "originalPrice": original,
            "currencyCode": "USD", "fmtPrice": {"originalPrice": fmt_original},
        }}

    def promo(active=None, upcoming=None):
        p = {}
        if active:
            p["promotionalOffers"] = [{"promotionalOffers": [active]}]
        if upcoming:
            p["upcomingPromotionalOffers"] = [{"promotionalOffers": [upcoming]}]
        return p

    elements = [
        {  # A — currently free
            "title": "Nova Free", "id": "A1", "namespace": "ns", "productSlug": "nova-free",
            "seller": {"name": "Studio"}, "keyImages": [{"type": "OfferImageWide", "url": "http://x/a.png"}],
            "price": price(0, 1999, "$19.99"),
            "promotions": promo(active={"startDate": past, "endDate": soon}),
        },
        {  # B — upcoming freebie (currently still priced)
            "title": "Future Free", "id": "B1", "namespace": "ns", "productSlug": "future-free",
            "price": price(2999, 2999, "$29.99"),
            "promotions": promo(upcoming={"startDate": soon, "endDate": later}),
        },
        {  # C — discounted but NOT free during an active promo → must be excluded
            "title": "Half Off Not Free", "id": "C1", "productSlug": "half-off",
            "price": price(999, 1999, "$19.99"),
            "promotions": promo(active={"startDate": past, "endDate": soon}),
        },
        {  # D — no promotions at all → excluded
            "title": "Regular Game", "id": "D1", "productSlug": "regular",
            "price": price(1999, 1999, "$19.99"), "promotions": {},
        },
    ]
    return {"data": {"Catalog": {"searchStore": {"elements": elements}}}}


def test_current_and_upcoming(monkeypatch):
    monkeypatch.setattr(epic, "_http_get_json", lambda *a, **k: _payload())
    data = epic.fetch_free_games(country="US", locale="en-US")

    current_titles = {g.title for g in data["current"]}
    upcoming_titles = {g.title for g in data["upcoming"]}

    assert current_titles == {"Nova Free"}  # only the genuinely-$0 one
    assert "Future Free" in upcoming_titles
    assert "Half Off Not Free" not in current_titles  # the $0 gate excludes a paid discount
    assert "Regular Game" not in current_titles | upcoming_titles


def test_current_game_fields(monkeypatch):
    monkeypatch.setattr(epic, "_http_get_json", lambda *a, **k: _payload())
    g = epic.current_free()[0]
    assert g.store == "epic"
    assert g.title == "Nova Free"
    assert g.original_price == "$19.99"
    assert g.original_price_cents == 1999
    assert g.url.endswith("/p/nova-free")
    assert g.is_current is True
    assert g.key == "epic:a1"  # store-scoped, lowercased


def test_network_failure_degrades(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(epic, "_http_get_json", boom)
    data = epic.fetch_free_games()  # must not raise
    assert data == {"current": [], "upcoming": []}
