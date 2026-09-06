"""Steam deals sourcing + alert-once state (no network — HTTP is monkeypatched)."""

import pytest

from navig_games.engine import settings as settings_mod
from navig_games.engine.deals_state import DealsState
from navig_games.engine.sources import steam


@pytest.fixture(autouse=True)
def _fresh_deals_cache():
    """Isolate every test from the module-level deals cache."""
    steam.clear_deals_cache()
    yield
    steam.clear_deals_cache()


def _canned():
    return {
        1: {"name": "Free To Keep", "is_free": False,
            "price_overview": {"discount_percent": 100, "final": 0, "initial": 1999,
                               "currency": "USD", "final_formatted": "Free"}},
        2: {"name": "Big Sale", "is_free": False,
            "price_overview": {"discount_percent": 75, "final": 500, "initial": 2000,
                               "currency": "USD", "final_formatted": "$5.00"}},
        3: {"name": "Small Sale", "is_free": False,
            "price_overview": {"discount_percent": 10, "final": 1800, "initial": 2000,
                               "currency": "USD", "final_formatted": "$18.00"}},
        4: {"name": "Always Free", "is_free": True},          # permanently free → skip
        5: {"name": "No Price", "is_free": False},            # no price_overview → skip
    }


def test_check_deals_detection(monkeypatch):
    canned = _canned()
    monkeypatch.setattr(steam, "resolve_steamid64", lambda: "123")
    monkeypatch.setattr(steam, "wishlist_appids", lambda sid: [1, 2, 3, 4, 5])
    monkeypatch.setattr(steam, "app_details", lambda appid, cc="us": canned.get(appid))
    monkeypatch.setattr(settings_mod, "get", lambda k, d=None: [] if k == "steam_watch" else d)

    r = steam.check_deals(threshold=20)
    names = [d.name for d in r["deals"]]
    assert "Free To Keep" in names       # free-to-keep promo
    assert "Big Sale" in names           # ≥ threshold
    assert "Small Sale" not in names     # below threshold
    assert "Always Free" not in names    # permanently free is not a deal
    assert "No Price" not in names       # no price data
    # free-to-keep sorts first
    assert r["deals"][0].is_free is True
    assert r["deals"][0].on_wishlist is True


def test_watched_appids_union(monkeypatch):
    monkeypatch.setattr(steam, "resolve_steamid64", lambda: "123")
    monkeypatch.setattr(steam, "wishlist_appids", lambda sid: [10, 20])
    monkeypatch.setattr(settings_mod, "get", lambda k, d=None: [20, 30] if k == "steam_watch" else d)
    ids = steam.watched_appids(extra=[40])
    assert ids == [10, 20, 30, 40]  # de-duped, order preserved


def test_check_deals_fetches_wishlist_once(monkeypatch):
    """Regression: check_deals used to fetch the wishlist twice + resolve 3×."""
    calls = {"wishlist": 0, "resolve": 0}

    def _wl(sid):
        calls["wishlist"] += 1
        return [1, 2]

    def _resolve():
        calls["resolve"] += 1
        return "123"

    monkeypatch.setattr(steam, "wishlist_appids", _wl)
    monkeypatch.setattr(steam, "resolve_steamid64", _resolve)
    monkeypatch.setattr(steam, "app_details", lambda a, cc="us": None)
    monkeypatch.setattr(settings_mod, "get", lambda k, d=None: [] if k == "steam_watch" else d)

    steam.check_deals()
    assert calls["wishlist"] == 1  # exactly one wishlist fetch
    assert calls["resolve"] == 1   # exactly one SteamID resolve


def test_check_deals_cache(monkeypatch):
    """A second call within the TTL is served from cache; use_cache=False and
    clear_deals_cache() force a fresh fetch."""
    calls = {"n": 0}

    def _wl(sid):
        calls["n"] += 1
        return [1]

    monkeypatch.setattr(steam, "resolve_steamid64", lambda: "123")
    monkeypatch.setattr(steam, "wishlist_appids", _wl)
    monkeypatch.setattr(steam, "app_details", lambda a, cc="us": None)
    monkeypatch.setattr(settings_mod, "get", lambda k, d=None: [] if k == "steam_watch" else d)

    steam.check_deals()  # miss → fetch
    steam.check_deals()  # hit → served from cache
    assert calls["n"] == 1

    steam.check_deals(use_cache=False)  # forced fresh
    assert calls["n"] == 2

    steam.clear_deals_cache()
    steam.check_deals()  # miss again after clear
    assert calls["n"] == 3


def test_check_deals_cache_keys_by_inputs(monkeypatch):
    """Different threshold/cc are distinct cache entries (no cross-contamination)."""
    calls = {"n": 0}
    monkeypatch.setattr(steam, "resolve_steamid64", lambda: "123")
    monkeypatch.setattr(steam, "wishlist_appids", lambda sid: (calls.__setitem__("n", calls["n"] + 1), [1])[1])
    monkeypatch.setattr(steam, "app_details", lambda a, cc="us": None)
    monkeypatch.setattr(settings_mod, "get", lambda k, d=None: [] if k == "steam_watch" else d)

    steam.check_deals(threshold=20)
    steam.check_deals(threshold=50)  # different key → fresh fetch
    steam.check_deals(threshold=20)  # back to first key → cached
    assert calls["n"] == 2


class _Deal:
    def __init__(self, appid, discount, is_free=False):
        self.appid, self.discount_pct, self.is_free = appid, discount, is_free


def test_deals_state_alert_once(tmp_path):
    st = DealsState(path=tmp_path / "d.json")
    d = _Deal(1, 50)
    assert st.should_alert(d)
    st.mark(d)
    st.save()

    reloaded = DealsState(path=tmp_path / "d.json")
    assert not reloaded.should_alert(_Deal(1, 50))          # same discount → no re-alert
    assert not reloaded.should_alert(_Deal(1, 40))          # worse → no
    assert reloaded.should_alert(_Deal(1, 60))              # deeper discount → yes
    assert reloaded.should_alert(_Deal(1, 50, is_free=True))  # newly free → yes


def test_deals_state_reset_absent(tmp_path):
    st = DealsState(path=tmp_path / "d.json")
    st.mark(_Deal(1, 50))
    st.mark(_Deal(2, 30))
    st.reset_absent([1])  # app 2's sale ended
    assert not st.should_alert(_Deal(1, 50))  # still remembered
    assert st.should_alert(_Deal(2, 30))      # cleared → a future sale re-alerts


# ── manual watchlist: names resolved in parallel, nothing dropped ─────────────


def test_watchlist_named_resolves_all_with_fallback(monkeypatch):
    monkeypatch.setattr(steam, "_manual_watchlist", lambda: [1245620, 413150, 999])
    names = {1245620: "ELDEN RING", 413150: "Stardew Valley"}  # 999 → None → fallback
    monkeypatch.setattr(steam, "app_details",
                        lambda a, cc="us": ({"name": names[a]} if a in names else None))

    out = steam.watchlist_named()
    assert [w["appid"] for w in out] == [1245620, 413150, 999]  # order preserved, all resolved
    assert out[0]["name"] == "ELDEN RING"
    assert out[2]["name"] == "App 999"  # best-effort fallback
    assert out[0]["url"].endswith("/app/1245620/")


def test_watchlist_named_empty_is_no_crash(monkeypatch):
    monkeypatch.setattr(steam, "_manual_watchlist", lambda: [])
    assert steam.watchlist_named() == []  # no ThreadPoolExecutor(max_workers=0)


def test_add_watch_concurrent_no_lost_update(monkeypatch):
    import threading

    store = {"wl": []}
    monkeypatch.setattr(steam, "_manual_watchlist", lambda: list(store["wl"]))
    monkeypatch.setattr(settings_mod, "set", lambda k, v: store.__setitem__("wl", v))

    threads = [threading.Thread(target=steam.add_watch, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(store["wl"]) == list(range(20))  # the lock prevents lost updates


# ── alert-once state survives two overlapping runs ───────────────────────────


class _Deal:
    def __init__(self, appid: int, pct: int, is_free: bool = False):
        self.appid, self.discount_pct, self.is_free = appid, pct, is_free


def test_two_overlapping_deals_runs_keep_each_others_marks(tmp_path):
    """The cron job and a manual `deals notify` overlap — neither may lose a mark.

    A whole-file save from a snapshot taken 30s earlier used to drop the other run's
    marks, re-announcing deals the user had already been told about.
    """
    from navig_games.engine.deals_state import DealsState

    path = tmp_path / "deals.json"
    cron, manual = DealsState(path=path), DealsState(path=path)  # both loaded empty

    cron.mark(_Deal(730, 50))
    cron.save()

    manual.mark(_Deal(440, 75))  # this run never saw app 730
    manual.save()

    fresh = DealsState(path=path)
    assert fresh.should_alert(_Deal(730, 50)) is False  # cron's mark survived
    assert fresh.should_alert(_Deal(440, 75)) is False  # and so did manual's


def test_the_deeper_discount_wins_on_merge(tmp_path):
    from navig_games.engine.deals_state import DealsState

    path = tmp_path / "deals.json"
    a, b = DealsState(path=path), DealsState(path=path)
    a.mark(_Deal(730, 80))
    a.save()
    b.mark(_Deal(730, 40))  # a shallower one, announced by the other run
    b.save()

    fresh = DealsState(path=path)
    assert fresh.should_alert(_Deal(730, 80)) is False  # 80 was already announced
    assert fresh.should_alert(_Deal(730, 90)) is True  # a better deal still alerts


def test_a_cleared_app_is_not_resurrected_by_the_merge(tmp_path):
    from navig_games.engine.deals_state import DealsState

    path = tmp_path / "deals.json"
    first = DealsState(path=path)
    first.mark(_Deal(730, 50))
    first.save()

    second = DealsState(path=path)  # sees 730
    second.reset_absent([])  # the sale ended
    second.save()

    assert DealsState(path=path).should_alert(_Deal(730, 50)) is True  # re-alerts
