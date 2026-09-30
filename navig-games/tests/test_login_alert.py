"""Proactive Epic sign-in alert — one deduped nudge instead of a daily drip.

Covers the alert-once state (`LoginAlertState`) and how `run_claim` wires it: a
dead session collapses to ONE `notify_session_expired` (not one `notify_result`
per game), stays quiet on repeat runs of the same batch, re-alerts for a new
week's batch, and re-arms once the session works again.
"""

import asyncio
import types

from navig_games.engine import last_run, login_state
from navig_games.engine import notify as notif
from navig_games.engine import runner
from navig_games.engine.claim.base import STATUS_CLAIMED, STATUS_MANUAL
from navig_games.engine.claim.base import ClaimResult
from navig_games.engine.login_state import LoginAlertState


# ── LoginAlertState (alert-once) ────────────────────────────────────────────
def test_state_alerts_once_per_batch(tmp_path):
    st = LoginAlertState(tmp_path / "a.json")
    keys = ["epic:luto", "epic:echo"]
    assert st.should_alert_expiry(keys) is True
    st.mark_expiry_alerted(keys)
    assert st.should_alert_expiry(keys) is False              # same batch → quiet
    assert st.should_alert_expiry(["epic:new"]) is True       # a new batch re-alerts


def test_state_is_order_independent(tmp_path):
    st = LoginAlertState(tmp_path / "a.json")
    st.mark_expiry_alerted(["b", "a"])
    assert st.should_alert_expiry(["a", "b"]) is False


def test_state_clear_rearms(tmp_path):
    st = LoginAlertState(tmp_path / "a.json")
    st.mark_expiry_alerted(["epic:luto"])
    st.clear_expiry()
    assert st.should_alert_expiry(["epic:luto"]) is True


def test_state_empty_batch_never_alerts(tmp_path):
    st = LoginAlertState(tmp_path / "a.json")
    assert st.should_alert_expiry([]) is False


def test_state_persists_across_instances(tmp_path):
    p = tmp_path / "a.json"
    LoginAlertState(p).mark_expiry_alerted(["epic:luto"])
    assert LoginAlertState(p).should_alert_expiry(["epic:luto"]) is False


# ── run_claim wiring ────────────────────────────────────────────────────────
def _run(coro):
    return asyncio.run(coro)


def _r(key, status, msg="x"):
    return ClaimResult(key, key.split(":")[-1].title(), status, "epic", msg,
                       url=f"https://store.epicgames.com/p/{key}")


def _wire(monkeypatch, tmp_path, results, login, keys):
    monkeypatch.setattr(login_state, "_state_path", lambda: tmp_path / "login_alert.json")
    monkeypatch.setattr(last_run, "record", lambda summary: None)

    cur = [types.SimpleNamespace(key=k, title=k) for k in keys]
    monkeypatch.setattr("navig_games.engine.sources.epic.fetch_free_games",
                        lambda **kw: {"current": cur, "upcoming": []})

    class _FakeLedger:
        def unseen(self, c):
            return c

        def record(self, *a, **k):
            pass

        def get(self, k):
            return None

    monkeypatch.setattr(runner, "Ledger", _FakeLedger)

    async def _run_epic(*a, **k):
        return results, login

    monkeypatch.setattr("navig_games.engine.claim.epic.run_epic_claims", _run_epic)

    calls = {"result": 0, "summary": 0, "expired": []}

    async def _nr(res, *a, **k):
        calls["result"] += 1

    async def _ns(cl, *a, **k):
        calls["summary"] += 1

    async def _ne(count, *a, **k):
        calls["expired"].append(count)

    monkeypatch.setattr(notif, "notify_result", _nr)
    monkeypatch.setattr(notif, "notify_summary", _ns)
    monkeypatch.setattr(notif, "notify_session_expired", _ne)
    return calls


def test_expired_session_collapses_to_one_alert(monkeypatch, tmp_path):
    results = [_r("epic:luto", STATUS_MANUAL, "not signed in"),
               _r("epic:echo", STATUS_MANUAL, "not signed in")]
    calls = _wire(monkeypatch, tmp_path, results, "needs_manual", ["epic:luto", "epic:echo"])

    _run(runner.run_claim(store="epic", force=True))

    assert calls["expired"] == [2]   # ONE alert for the whole batch…
    assert calls["result"] == 0      # …not a per-game "needs you" ping
    assert calls["summary"] == 0


def test_expired_session_is_deduped_on_repeat_run(monkeypatch, tmp_path):
    results = [_r("epic:luto", STATUS_MANUAL), _r("epic:echo", STATUS_MANUAL)]
    calls = _wire(monkeypatch, tmp_path, results, "needs_manual", ["epic:luto", "epic:echo"])

    _run(runner.run_claim(store="epic", force=True))   # alerts
    _run(runner.run_claim(store="epic", force=True))   # same batch → quiet

    assert calls["expired"] == [2]   # exactly once across both runs


def test_new_batch_realerts_while_still_expired(monkeypatch, tmp_path):
    # Week 1: signed out, one freebie → alert.
    calls1 = _wire(monkeypatch, tmp_path, [_r("epic:luto", STATUS_MANUAL)],
                   "needs_manual", ["epic:luto"])
    _run(runner.run_claim(store="epic", force=True))
    assert calls1["expired"] == [1]

    # Week 2: still signed out, but a NEW freebie (same tmp state file) → re-alert.
    calls2 = _wire(monkeypatch, tmp_path, [_r("epic:brand-new", STATUS_MANUAL)],
                   "needs_manual", ["epic:brand-new"])
    _run(runner.run_claim(store="epic", force=True))
    assert calls2["expired"] == [1]


def test_healthy_run_notifies_per_game_and_rearms(monkeypatch, tmp_path):
    # First: expired → alert + mark state.
    exp = [_r("epic:luto", STATUS_MANUAL)]
    calls = _wire(monkeypatch, tmp_path, exp, "needs_manual", ["epic:luto"])
    _run(runner.run_claim(store="epic", force=True))
    assert calls["expired"] == [1]

    # Then the user re-logs in and it claims: per-game notify returns, state clears.
    ok = [_r("epic:luto", STATUS_CLAIMED, "claimed")]
    calls2 = _wire(monkeypatch, tmp_path, ok, "session_restored", ["epic:luto"])
    _run(runner.run_claim(store="epic", force=True))
    assert calls2["result"] == 1
    assert calls2["summary"] == 1
    assert calls2["expired"] == []
    # Re-armed: a fresh expiry for the same game alerts again.
    assert LoginAlertState(tmp_path / "login_alert.json").should_alert_expiry(["epic:luto"]) is True


def test_dry_run_never_alerts(monkeypatch, tmp_path):
    results = [_r("epic:luto", STATUS_MANUAL)]
    calls = _wire(monkeypatch, tmp_path, results, "needs_manual", ["epic:luto"])
    _run(runner.run_claim(store="epic", force=True, dry_run=True))
    assert calls["expired"] == []
