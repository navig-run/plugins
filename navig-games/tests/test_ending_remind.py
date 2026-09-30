"""The scheduled 'ending soon' reminder — a one-time nudge for un-grabbed
giveaways about to expire, wired into the daily deals run.
"""

from navig_games.engine import deals_state, expiry_state
from navig_games.engine import notify as notif
from navig_games.engine import runner
from navig_games.engine.expiry_state import ExpiryReminderState
from navig_games.engine.models import FreeGame
from navig_games.engine.sources import giveaways as gw_src
from navig_games.engine.sources import steam as steam_src


# ── ExpiryReminderState ─────────────────────────────────────────────────────
def test_state_reminds_once(tmp_path):
    st = ExpiryReminderState(tmp_path / "e.json")
    assert st.should_remind("itch:1") is True
    st.mark("itch:1")
    assert st.should_remind("itch:1") is False


def test_state_prune_drops_absent(tmp_path):
    st = ExpiryReminderState(tmp_path / "e.json")
    st.mark("itch:1")
    st.prune(["itch:2"])            # itch:1 no longer on offer
    assert st.should_remind("itch:1") is True   # forgotten → could remind again


def test_state_persists(tmp_path):
    p = tmp_path / "e.json"
    s1 = ExpiryReminderState(p)
    s1.mark("itch:1")
    s1.save()
    assert ExpiryReminderState(p).should_remind("itch:1") is False


# ── run_deals_notify reminder pass ──────────────────────────────────────────
def _g(slug, ends, store="itch"):
    return FreeGame(store=store, title=slug.title(), slug=slug, ends_at=ends,
                    url=f"https://x/{slug}", original_price="$5")


def _wire(monkeypatch, tmp_path, giveaways, *, known=(), settled=(), day_by_ends=None):
    # Steam deals + free-to-keep: empty (isolate the giveaway path).
    monkeypatch.setattr(steam_src, "check_deals", lambda **k: {"deals": [], "checked": 0})
    monkeypatch.setattr(steam_src, "free_to_keep", lambda **k: [])
    monkeypatch.setattr(gw_src, "fetch", lambda **k: list(giveaways))

    class _DS:  # no-op deals state (no real file writes)
        def should_alert(self, d):
            return False

        def mark(self, d):
            pass

        def reset_absent(self, a):
            pass

        def save(self):
            pass

    monkeypatch.setattr(deals_state, "DealsState", _DS)

    known_set, settled_set = set(known), set(settled)

    class _Ledger:
        def get(self, k):
            return {"status": "needs_manual"} if k in known_set else None

        def is_settled(self, k):
            return k in settled_set

        def record(self, *a, **k):
            pass

    monkeypatch.setattr(runner, "Ledger", _Ledger)
    monkeypatch.setattr(runner, "days_until", lambda iso: (day_by_ends or {}).get(iso))
    monkeypatch.setattr(expiry_state, "_state_path", lambda: tmp_path / "exp.json")

    calls = {"ending": [], "fresh": []}

    async def _ne(games, *a, **k):
        calls["ending"].append([g.key for g in games])

    async def _nf(games, *a, **k):
        calls["fresh"].append([g.key for g in games])

    monkeypatch.setattr(notif, "notify_ending_soon", _ne)
    monkeypatch.setattr(notif, "notify_free_to_keep", _nf)
    return calls


def test_known_unsettled_imminent_is_reminded_once(monkeypatch, tmp_path):
    gv = [_g("one", "E1")]
    calls = _wire(monkeypatch, tmp_path, gv, known=["itch:one"], day_by_ends={"E1": 1})

    out = runner.run_deals_notify()
    assert calls["ending"] == [["itch:one"]]
    assert out["ending_soon"] == ["One"]

    # A second run with the same state file → no repeat.
    calls2 = _wire(monkeypatch, tmp_path, gv, known=["itch:one"], day_by_ends={"E1": 1})
    out2 = runner.run_deals_notify()
    assert calls2["ending"] == []
    assert out2["ending_soon"] == []


def test_fresh_imminent_is_not_double_notified(monkeypatch, tmp_path):
    # New this run (ledger.get is None → fresh) → gets the "new" notify, NOT ending-soon.
    gv = [_g("two", "E1")]
    calls = _wire(monkeypatch, tmp_path, gv, known=[], day_by_ends={"E1": 1})
    out = runner.run_deals_notify()
    assert calls["fresh"] == [["itch:two"]]
    assert calls["ending"] == []
    assert out["ending_soon"] == []


def test_settled_imminent_is_not_reminded(monkeypatch, tmp_path):
    gv = [_g("three", "E1")]
    calls = _wire(monkeypatch, tmp_path, gv, known=["itch:three"], settled=["itch:three"],
                  day_by_ends={"E1": 1})
    runner.run_deals_notify()
    assert calls["ending"] == []


def test_far_off_is_not_reminded(monkeypatch, tmp_path):
    gv = [_g("four", "E5")]
    calls = _wire(monkeypatch, tmp_path, gv, known=["itch:four"], day_by_ends={"E5": 5})
    runner.run_deals_notify()
    assert calls["ending"] == []


# ── notify_ending_soon formatting ───────────────────────────────────────────
def test_notify_ending_soon_batches_and_ranks(monkeypatch):
    import asyncio

    sent = {}

    async def _dispatch(type_key, title, body, *, priority=None, data=None):
        sent.update(title=title, body=body, priority=priority, data=data)

    import navig.notify as core_notify

    monkeypatch.setattr(core_notify, "dispatch", _dispatch)
    monkeypatch.setattr("navig_games.engine.models.days_until",
                        lambda iso, **k: {"LATER": 2, "NOW": 0}.get(iso))

    games = [_g("later", "LATER"), _g("now", "NOW")]
    asyncio.run(notif.notify_ending_soon(games))

    assert "⏰ 2 free games ending soon" in sent["title"]
    assert sent["priority"] == "high"
    # Soonest first: "now" (today) ranks before "later".
    assert sent["body"].index("Now") < sent["body"].index("Later")
    assert "today" in sent["body"] and "in 2 days" in sent["body"]
