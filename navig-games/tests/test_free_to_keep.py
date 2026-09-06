"""Steam free-to-keep giveaways — the source behind the community 'free Steam games'
bots, sourced natively from Steam's own keyless store search.

The classifier is the safety-critical part: a permanently FREE-TO-PLAY title must
never be announced as a giveaway.
"""

import pytest

from navig_games.engine.sources import steam


@pytest.fixture(autouse=True)
def _fresh_free_cache():
    steam.clear_free_cache()
    yield
    steam.clear_free_cache()


# ── appid recovery (the search JSON carries no appid) ────────────────────────


def test_appid_from_capsule_url():
    url = "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/1623730/abc/capsule_sm_120.jpg?t=1"
    assert steam._appid_from_capsule(url) == 1623730


@pytest.mark.parametrize("bad", ["", None, "https://example.com/no/appid/here.jpg"])
def test_appid_from_capsule_missing(bad):
    assert steam._appid_from_capsule(bad) is None


# ── the classifier: giveaway vs free-to-play vs paid ─────────────────────────


def _paid_100_off():
    return {"name": "Giveaway Game", "is_free": False, "header_image": "img",
            "price_overview": {"final": 0, "initial": 1999, "discount_percent": 100,
                               "initial_formatted": "$19.99", "currency": "USD"}}


def test_classifier_accepts_paid_game_at_100_off():
    g = steam._free_to_keep_from(42, _paid_100_off())
    assert g is not None
    assert g.store == "steam" and g.title == "Giveaway Game"
    assert g.key == "steam:42"  # store-scoped ledger identity
    assert g.original_price == "$19.99" and g.original_price_cents == 1999
    assert g.url.endswith("/app/42/")


def test_classifier_refuses_free_to_play():
    # The whole point: CS2-style F2P titles are NOT giveaways.
    f2p = {"name": "Free To Play Game", "is_free": True}
    assert steam._free_to_keep_from(730, f2p) is None


def test_classifier_refuses_paid_game_not_free():
    d = _paid_100_off()
    d["price_overview"] = {"final": 5999, "initial": 5999, "discount_percent": 0}
    assert steam._free_to_keep_from(1245620, d) is None


def test_classifier_refuses_partial_discount():
    d = _paid_100_off()
    d["price_overview"] = {"final": 999, "initial": 1999, "discount_percent": 50}
    assert steam._free_to_keep_from(7, d) is None


def test_classifier_refuses_missing_details():
    assert steam._free_to_keep_from(7, None) is None


# ── end-to-end sourcing (network monkeypatched) ──────────────────────────────


def test_free_to_keep_sources_and_verifies(monkeypatch):
    search = {"items": [
        {"name": "Giveaway Game", "logo": ".../apps/42/x/capsule_sm_120.jpg"},
        {"name": "Some F2P", "logo": ".../apps/730/x/capsule_sm_120.jpg"},
    ]}
    monkeypatch.setattr(steam, "_get_json", lambda url, params, timeout=20: search)
    details = {42: _paid_100_off(), 730: {"name": "Some F2P", "is_free": True}}
    monkeypatch.setattr(steam, "app_details", lambda a, cc="us": details.get(a))

    out = steam.free_to_keep(use_cache=False)
    assert [g.title for g in out] == ["Giveaway Game"]  # the F2P title is filtered out


def test_free_to_keep_empty_search_is_no_crash(monkeypatch):
    monkeypatch.setattr(steam, "_get_json", lambda url, params, timeout=20: {"items": []})
    assert steam.free_to_keep(use_cache=False) == []


def test_free_to_keep_survives_a_sourcing_failure(monkeypatch):
    def _boom(url, params, timeout=20):
        raise RuntimeError("steam down")

    monkeypatch.setattr(steam, "_get_json", _boom)
    assert steam.free_to_keep(use_cache=False) == []  # never breaks the Free feed


def test_free_to_keep_is_cached(monkeypatch):
    calls = {"n": 0}

    def _search(url, params, timeout=20):
        calls["n"] += 1
        return {"items": []}

    monkeypatch.setattr(steam, "_get_json", _search)
    steam.free_to_keep()
    steam.free_to_keep()
    assert calls["n"] == 1  # second call served from cache


# ── multi-store Free feed (Epic + Steam) ─────────────────────────────────────


def test_check_merges_epic_and_steam(monkeypatch):
    from navig_games.engine import runner
    from navig_games.engine.ledger import Ledger
    from navig_games.engine.models import FreeGame
    from navig_games.engine.sources import giveaways

    monkeypatch.setattr(giveaways, "fetch", lambda **kw: [])  # isolate: no network

    epic_game = FreeGame(store="epic", title="Nova Lands", slug="nova")
    steam_game = FreeGame(store="steam", title="Giveaway Game", slug="42")
    monkeypatch.setattr("navig_games.engine.sources.epic.fetch_free_games",
                        lambda country=None, locale=None: {"current": [epic_game], "upcoming": []})
    monkeypatch.setattr(steam, "free_to_keep", lambda cc="us": [steam_game])
    monkeypatch.setattr(Ledger, "get", lambda self, k: None)

    out = runner.check()  # default: all stores
    assert [(g["store"], g["title"]) for g in out["current"]] == [
        ("epic", "Nova Lands"), ("steam", "Giveaway Game"),
    ]


def test_check_steam_failure_never_breaks_the_epic_feed(monkeypatch):
    from navig_games.engine import runner
    from navig_games.engine.ledger import Ledger
    from navig_games.engine.models import FreeGame
    from navig_games.engine.sources import giveaways

    monkeypatch.setattr(giveaways, "fetch", lambda **kw: [])  # isolate: no network

    monkeypatch.setattr("navig_games.engine.sources.epic.fetch_free_games",
                        lambda country=None, locale=None:
                        {"current": [FreeGame(store="epic", title="Nova", slug="n")], "upcoming": []})

    def _boom(cc="us"):
        raise RuntimeError("steam down")

    monkeypatch.setattr(steam, "free_to_keep", _boom)
    monkeypatch.setattr(Ledger, "get", lambda self, k: None)

    out = runner.check()
    assert [g["store"] for g in out["current"]] == ["epic"]  # Epic still served
