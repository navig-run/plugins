"""The proactive monitor must settle 'seen' on VERIFIED delivery, never mark-then-drop.

`_monitor` alerts on new important mail via `_dispatch` (best-effort, swallows a notify-router
failure). The bug: it marked EVERY new email `seen` regardless of whether its `email_important`
alert was delivered — so an important email whose dispatch failed (router down) was silently
dropped forever, never re-notified. Same verified-delivery class as the briefings fix (#677) and
the reminder poller. Fix: on a seeded run, mark seen only what was HANDLED — no rule matched
(nothing to deliver) or the alert was delivered; a failed alert stays unseen and retries next tick.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Test THIS checkout's plugin, not whichever copy pip installed editable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from navig_email import service as S  # noqa: E402


def _async(value):
    async def _f(*_a, **_k):
        return value

    return _f


def _msg(mid: str) -> dict:
    return {"id": mid, "subject": f"s-{mid}", "from": "sender", "snippet": "x", "url": f"u-{mid}"}


def _run_monitor(monkeypatch, *, msgs, matching, dispatch, seeded, seen=None):
    """Drive _monitor with stubbed gmail/rules/dispatch; return the list of update_state kwargs."""
    c = {
        "monitor_enabled": True,
        "rules": [{"enabled": True, "name": "r"}],
        "state": {"seen_ids": list(seen or []), "seeded": seeded},
    }
    monkeypatch.setattr(S.gmail, "search", _async(msgs))
    monkeypatch.setattr(S.rules, "needs_body", lambda _rl: False)
    monkeypatch.setattr(
        S.rules, "first_match",
        lambda m, _rl: {"name": "r", "channels": ["deck"]} if m["id"] in matching else None,
    )
    if callable(dispatch):
        async def _disp(_t, _ti, _b, _ch, priority="normal", data=None):
            return dispatch(data or {})
        monkeypatch.setattr(S, "_dispatch", _disp)
    else:
        monkeypatch.setattr(S, "_dispatch", _async(dispatch))
    updates: list[dict] = []
    monkeypatch.setattr(S.cfg, "update_state", lambda **kw: updates.append(kw))
    asyncio.run(S.EmailService()._monitor(c))
    return updates


# ── the core regression: a failed important-email alert must NOT be marked seen ──

def test_monitor_leaves_unseen_on_dispatch_failure(monkeypatch):
    updates = _run_monitor(monkeypatch, msgs=[_msg("a")], matching={"a"}, dispatch=False, seeded=True)
    # nothing handled → no state write → 'a' stays unseen and retries next tick
    assert updates == []


def test_monitor_marks_seen_on_delivery(monkeypatch):
    updates = _run_monitor(monkeypatch, msgs=[_msg("a")], matching={"a"}, dispatch=True, seeded=True)
    assert updates and "a" in updates[0]["seen_ids"]


def test_monitor_marks_non_matching_seen(monkeypatch):
    # an email that matches no rule has nothing to deliver → mark seen so we don't re-fetch it forever
    updates = _run_monitor(monkeypatch, msgs=[_msg("a")], matching=set(), dispatch=False, seeded=True)
    assert updates and "a" in updates[0]["seen_ids"]


def test_monitor_partial_delivery_marks_only_delivered(monkeypatch):
    # 'a' delivers, 'b' fails → only 'a' is marked seen; 'b' retries
    updates = _run_monitor(
        monkeypatch, msgs=[_msg("a"), _msg("b")], matching={"a", "b"},
        dispatch=lambda data: data.get("url") == "u-a", seeded=True,
    )
    assert updates
    seen = updates[0]["seen_ids"]
    assert "a" in seen and "b" not in seen


def test_monitor_first_run_seeds_all_without_notifying(monkeypatch):
    calls: list = []

    async def _disp(*a, **k):
        calls.append(a)
        return True

    monkeypatch.setattr(S, "_dispatch", _disp)
    monkeypatch.setattr(S.gmail, "search", _async([_msg("a"), _msg("b")]))
    monkeypatch.setattr(S.rules, "needs_body", lambda _rl: False)
    monkeypatch.setattr(S.rules, "first_match", lambda m, _rl: {"name": "r", "channels": ["deck"]})
    updates: list[dict] = []
    monkeypatch.setattr(S.cfg, "update_state", lambda **kw: updates.append(kw))
    c = {"monitor_enabled": True, "rules": [{"enabled": True, "name": "r"}], "state": {"seeded": False}}
    asyncio.run(S.EmailService()._monitor(c))

    assert calls == []  # first run never notifies the backlog
    assert updates and updates[0]["seeded"] is True
    seen = updates[0]["seen_ids"]
    assert "a" in seen and "b" in seen
