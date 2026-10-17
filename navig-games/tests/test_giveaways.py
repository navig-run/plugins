"""Cross-store giveaway feed (itch / GOG / IndieGala / Ubisoft …).

The two safety-critical behaviours: entries for stores we source NATIVELY (Epic,
Steam) must be dropped so the Free tab can't show a game twice, and non-PC /
inactive / non-game entries must never reach the feed.
"""

import pytest

from navig_games.engine.sources import giveaways as gw


@pytest.fixture(autouse=True)
def _fresh_cache():
    gw.clear_cache()
    yield
    gw.clear_cache()


def _item(**over):
    base = {
        "id": 3096,
        "title": "Madness Inside (itch.io) Giveaway",
        "worth": "$6.99",
        "thumbnail": "thumb.jpg",
        "description": "A spooky game",
        "open_giveaway_url": "https://www.gamerpower.com/open/madness-inside",
        "type": "Game",
        "platforms": "PC, Itch.io, DRM-Free",
        "end_date": "2026-08-01 23:59:59",
        "status": "Active",
    }
    base.update(over)
    return base


# ── store normalisation (the feed hides the store in platforms *or* the title) ──


@pytest.mark.parametrize(
    "platforms,title,expected",
    [
        ("PC, Itch.io, DRM-Free", "X (itch.io) Giveaway", "itch"),
        ("PC, DRM-Free", "X (IndieGala) Giveaway", "indiegala"),
        ("PC", "GigaBash (Stove) Giveaway", "stove"),
        ("PC, GOG", "X Giveaway", "gog"),
        ("PC, Ubisoft Connect", "X Giveaway", "ubisoft"),
        ("PC, DRM-Free", "Nameless Giveaway", "drm-free"),  # no store hint anywhere
        ("PC", "Nameless Giveaway", "pc"),
    ],
)
def test_store_normalisation(platforms, title, expected):
    assert gw._store_of(platforms, title) == expected


def test_title_is_cleaned_of_store_tag_and_giveaway_suffix():
    assert gw._clean_title("Madness Inside (itch.io) Giveaway") == "Madness Inside"
    assert gw._clean_title("GigaBash (Stove) Giveaway") == "GigaBash"
    assert gw._clean_title("Plain Title") == "Plain Title"


def test_open_ended_giveaway_has_no_end_date():
    assert gw._ends_at("N/A") == ""
    assert gw._ends_at("2026-08-01 23:59:59") == "2026-08-01T23:59:59"


# ── the exclusion rules ───────────────────────────────────────────────────────


def test_maps_a_valid_itch_giveaway():
    g = gw._to_game(_item())
    assert g is not None
    assert g.store == "itch" and g.title == "Madness Inside"
    assert g.key == "itch:3096"  # stable ledger identity (store-scoped)
    assert g.original_price == "$6.99"
    assert g.url.endswith("/open/madness-inside")


@pytest.mark.parametrize(
    "platforms,title",
    [("PC, Epic Games Store", "Nova Lands (Epic Games) Giveaway"),
     ("PC, Steam", "Some Game (Steam) Giveaway")],
)
def test_drops_natively_sourced_stores(platforms, title):
    # Epic + Steam have their own authoritative sources (and Epic is auto-claimable),
    # so the feed must never duplicate them into the Free tab.
    assert gw._to_game(_item(platforms=platforms, title=title)) is None


def test_drops_inactive_and_non_game_and_non_pc():
    assert gw._to_game(_item(status="Expired")) is None
    assert gw._to_game(_item(type="DLC")) is None          # loot/DLC/beta ≠ a free game
    assert gw._to_game(_item(platforms="Android, iOS")) is None  # not a PC library game


def test_drops_entry_without_a_url():
    assert gw._to_game(_item(open_giveaway_url="", gamerpower_url="")) is None


# ── fetch(): filtering, caching, failure-safety ───────────────────────────────


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


def test_fetch_filters_and_maps(monkeypatch):
    payload = [
        _item(),  # itch → kept
        _item(id=1, platforms="PC, Epic Games Store", title="Nova (Epic Games) Giveaway"),  # dropped
        _item(id=2, status="Expired"),  # dropped
    ]
    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(payload))
    out = gw.fetch(use_cache=False)
    assert [g.title for g in out] == ["Madness Inside"]


def test_fetch_survives_a_failure(monkeypatch):
    import requests

    def _boom(*a, **k):
        raise RuntimeError("gamerpower down")

    monkeypatch.setattr(requests, "get", _boom)
    assert gw.fetch(use_cache=False) == []  # never breaks the Free feed


def test_fetch_survives_a_non_list_payload(monkeypatch):
    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp({"error": "nope"}))
    assert gw.fetch(use_cache=False) == []


def test_fetch_is_cached(monkeypatch):
    calls = {"n": 0}
    import requests

    def _get(*a, **k):
        calls["n"] += 1
        return _Resp([])

    monkeypatch.setattr(requests, "get", _get)
    gw.fetch()
    gw.fetch()
    assert calls["n"] == 1  # second call served from cache


# ── "what it takes" — the real instructions, carried through honestly ─────────


def test_one_line_collapses_ragged_whitespace():
    raw = "1. Visit the page.\n  2. Log in to your free\tIndieGala account.\n\n 3. Add to library."
    assert gw._one_line(raw) == (
        "1. Visit the page. 2. Log in to your free IndieGala account. 3. Add to library."
    )
    assert gw._one_line(None) == ""
    assert gw._one_line("") == ""


def test_instructions_are_carried_onto_the_game():
    g = gw._to_game(_item(instructions="1. Visit.\n2. Log in to itch.io.\n3. Claim."))
    assert g.instructions == "1. Visit. 2. Log in to itch.io. 3. Claim."


def test_missing_instructions_is_empty_not_none():
    g = gw._to_game(_item(instructions=None))
    assert g.instructions == ""  # empty string keeps the UI/type simple


def test_native_sources_carry_no_instructions():
    # Epic/Steam are claimed or linked directly — no "what it takes" copy needed, and
    # the field must stay empty so the UI doesn't render a blank line for them.
    from navig_games.engine.models import FreeGame

    assert FreeGame(store="epic", title="X").instructions == ""
