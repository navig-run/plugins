"""Email briefings must settle on VERIFIED delivery, never phantom-complete.

`_dispatch` swallows a notify-router failure (best-effort, never breaks the loop). The bug:
`_briefings` marked the period done and `run_brief_now` reported `sent: True` regardless — so a
brief whose delivery failed was silently dropped for the whole day/week/month, with no retry. The
same "settle on verified delivery, not phantom-complete" class as the reminder poller.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

# Test THIS checkout's plugin, not whichever copy pip installed editable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from navig_email import service as S  # noqa: E402


def _async(value):
    async def _f(*_a, **_k):
        return value

    return _f


def _due_briefing():
    now = datetime.now()
    return {"id": "b1", "name": "Brief", "cadence": "daily", "hour": now.hour,
            "enabled": True, "channels": ["deck"]}


# ── _dispatch returns the real outcome ────────────────────────────────────────

def test_dispatch_true_on_success(monkeypatch):
    class _R:
        async def dispatch(self, *_a, **_k):
            return None

    monkeypatch.setattr("navig.notify.router.get_notification_router", lambda: _R())
    assert asyncio.run(S._dispatch("t", "ti", "b", None)) is True


def test_dispatch_false_when_router_raises(monkeypatch):
    class _R:
        async def dispatch(self, *_a, **_k):
            raise RuntimeError("router down")

    monkeypatch.setattr("navig.notify.router.get_notification_router", lambda: _R())
    assert asyncio.run(S._dispatch("t", "ti", "b", None)) is False


# ── _briefings only marks the period on verified delivery ─────────────────────

def _run_briefings(monkeypatch, *, brief_text, delivered):
    c = {"briefings": [_due_briefing()], "state": {"last_brief": {}}}
    monkeypatch.setattr(S, "build_brief", _async(brief_text))
    monkeypatch.setattr(S, "_dispatch", _async(delivered))
    updates: list[dict] = []
    monkeypatch.setattr(S.cfg, "update_state", lambda **kw: updates.append(kw))
    asyncio.run(S.EmailService()._briefings(c))
    return updates


def test_briefing_not_marked_when_dispatch_fails(monkeypatch):
    updates = _run_briefings(monkeypatch, brief_text="a brief", delivered=False)
    assert updates == []  # delivery failed → NOT marked → retries next tick within the hour


def test_briefing_marked_when_delivered(monkeypatch):
    updates = _run_briefings(monkeypatch, brief_text="a brief", delivered=True)
    assert updates and updates[0]["last_brief"]["b1"]  # marked complete for the period


def test_briefing_marked_when_no_emails(monkeypatch):
    # empty brief = nothing to deliver → mark done so we don't rebuild it every tick
    updates = _run_briefings(monkeypatch, brief_text="", delivered=True)
    assert updates and updates[0]["last_brief"]["b1"]


# ── run_brief_now ("Send now") reports the real outcome ───────────────────────

def _run_brief_now(monkeypatch, *, delivered):
    monkeypatch.setattr(S.cfg, "load_config",
                        lambda: {"briefings": [{"id": "b1", "enabled": True, "cadence": "daily", "channels": ["deck"]}]})
    monkeypatch.setattr(S, "build_brief", _async("a brief"))
    monkeypatch.setattr(S, "_dispatch", _async(delivered))
    return asyncio.run(S.EmailService().run_brief_now("b1"))


def test_run_brief_now_reports_delivery_failure(monkeypatch):
    r = _run_brief_now(monkeypatch, delivered=False)
    assert r["sent"] is False and r["ok"] is False and "note" in r


def test_run_brief_now_reports_success(monkeypatch):
    r = _run_brief_now(monkeypatch, delivered=True)
    assert r["sent"] is True and r["ok"] is True
