"""Input validation + exit codes for `navig calendar` — a bad --start must fail
fast with a clean message (not a traceback), --duration must be positive, and
error paths must exit non-zero so automation can detect failure. No network: the
start-time parse happens up front, before any provider call.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import navig.agent.proactive as proactive_pkg
import navig.config as ncfg
from navig.agent.proactive.providers import CalendarEvent
from typer.testing import CliRunner

from navig_calendar.commands.calendar import calendar_app

# These stubs deliberately provide `get_global_config`, NOT
# `_load_global_config`. The latter returns the pydantic-VALIDATED config view,
# which does not declare `proactive` -- so the command under test could only
# ever see {} and reported the calendar as disabled regardless of config.
# Stubbing the broken reader is what let that ship; see
# core/tests/quality/test_validated_config_view_keys.py.

runner = CliRunner()


def _use_mock_provider(monkeypatch, provider_cls):
    # `{}` config → calendar not enabled → the command builds MockCalendar(); swap it.
    monkeypatch.setattr(
        ncfg, "get_config_manager",
        lambda: type("CM", (), {"get_global_config": lambda self: {}})(),
    )
    monkeypatch.setattr(proactive_pkg, "MockCalendar", provider_cls)


def test_add_bad_start_fails_cleanly():
    r = runner.invoke(calendar_app, ["add", "Meeting", "--start", "not-a-date"])
    # typer.Exit(2), NOT an uncaught ValueError (which CliRunner reports as exit 1).
    assert r.exit_code == 2
    assert not isinstance(r.exception, ValueError)
    assert "Traceback (most recent call last)" not in r.output


def test_add_duration_must_be_positive():
    r = runner.invoke(calendar_app, ["add", "Meeting", "--duration", "0"])
    assert r.exit_code == 2  # typer min=1 rejects zero/negative before the body runs


def test_auth_unknown_provider_exits_nonzero():
    r = runner.invoke(calendar_app, ["auth", "outlook"])
    assert r.exit_code == 1  # was: printed an error but exited 0


def test_add_valid_start_non_caldav_exits_one(monkeypatch):
    # A valid ISO --start must PARSE (no crash); with a non-CalDAV provider the
    # command must then exit 1 (not silently return success).
    monkeypatch.setattr(
        ncfg, "get_config_manager",
        lambda: type("CM", (), {"get_global_config": lambda self: {}})(),
    )
    r = runner.invoke(calendar_app, ["add", "Meeting", "--start", "2026-07-20T14:30"])
    assert r.exit_code == 1
    assert not isinstance(r.exception, ValueError)


def test_list_sorts_events_by_start(monkeypatch):
    # ICS/CalDAV return provider order, not time order — `list` must sort so `-n` really
    # shows the SOONEST events. Provider yields them out of order (c, a, b).
    base = datetime(2026, 7, 23)

    class _Unsorted:
        async def list_events(self, start, end):
            return [
                CalendarEvent(id="c", title="C", start=base.replace(hour=12), end=base.replace(hour=13)),
                CalendarEvent(id="a", title="A", start=base.replace(hour=8), end=base.replace(hour=9)),
                CalendarEvent(id="b", title="B", start=base.replace(hour=10), end=base.replace(hour=11)),
            ]

    _use_mock_provider(monkeypatch, _Unsorted)
    r = runner.invoke(calendar_app, ["list", "--json"])
    assert r.exit_code == 0, r.output
    assert [e["id"] for e in json.loads(r.output)] == ["a", "b", "c"]  # sorted by start


def test_list_mixed_naive_and_aware_starts_does_not_crash(monkeypatch):
    # A feed mixing floating (naive) and tz-aware times must sort without a TypeError.
    base = datetime(2026, 7, 23)

    class _Mixed:
        async def list_events(self, start, end):
            return [
                CalendarEvent(id="naive", title="N", start=base.replace(hour=12), end=base.replace(hour=13)),
                CalendarEvent(id="aware", title="A",
                              start=base.replace(hour=8, tzinfo=timezone.utc),
                              end=base.replace(hour=9, tzinfo=timezone.utc)),
            ]

    _use_mock_provider(monkeypatch, _Mixed)
    r = runner.invoke(calendar_app, ["list", "--json"])
    assert r.exit_code == 0, r.output
    assert {e["id"] for e in json.loads(r.output)} == {"naive", "aware"}


def _use_caldav(monkeypatch, provider_cls):
    """Config → CalDAV so `add` reaches the provider, and swap in a fake provider."""
    monkeypatch.setattr(
        ncfg, "get_config_manager",
        lambda: type("CM", (), {"get_global_config": lambda self: {
            "proactive": {"calendar": {
                "provider": "caldav", "url": "https://dav.example/",
                "username": "u", "password": "p"}}}})(),
    )
    monkeypatch.setattr(proactive_pkg, "CalDAVProvider", provider_cls)


def test_add_caldav_calls_create_event_not_the_missing_add_event(monkeypatch):
    """Regression: the command called provider.add_event, which no provider defines —
    so `navig calendar add` always raised AttributeError with a real CalDAV server. It
    must call create_event (the abstract method every provider implements)."""
    captured: dict = {}

    class _FakeCalDAV:
        # deliberately NO add_event — reverting the fix → AttributeError → exit != 0
        def __init__(self, url=None, username=None, password=None):
            captured["ctor"] = (url, username, password)

        async def create_event(self, event):
            captured["event"] = event
            return "evt-123"

    _use_caldav(monkeypatch, _FakeCalDAV)
    r = runner.invoke(
        calendar_app, ["add", "Meeting", "--start", "2026-07-20T14:30", "--duration", "30"]
    )
    assert r.exit_code == 0, r.output
    ev = captured["event"]
    assert ev.title == "Meeting"
    # a naive --start must be pinned to a real instant (not a floating iCal time)
    assert ev.start.tzinfo is not None
    assert ev.end.tzinfo is not None
    assert (ev.end - ev.start).total_seconds() == 30 * 60


def test_add_caldav_default_now_is_tz_aware(monkeypatch):
    """No --start → datetime.now() must also be pinned tz-aware, not left floating."""
    captured: dict = {}

    class _FakeCalDAV:
        def __init__(self, **kw):
            pass

        async def create_event(self, event):
            captured["event"] = event
            return "x"

    _use_caldav(monkeypatch, _FakeCalDAV)
    r = runner.invoke(calendar_app, ["add", "Reminder"])
    assert r.exit_code == 0, r.output
    assert captured["event"].start.tzinfo is not None


def test_add_caldav_preserves_an_explicit_offset(monkeypatch):
    """An explicit UTC offset in --start is preserved (not clobbered by local coercion)."""
    captured: dict = {}

    class _FakeCalDAV:
        def __init__(self, **kw):
            pass

        async def create_event(self, event):
            captured["event"] = event
            return "x"

    _use_caldav(monkeypatch, _FakeCalDAV)
    r = runner.invoke(calendar_app, ["add", "Sync", "--start", "2026-07-20T14:30+00:00"])
    assert r.exit_code == 0, r.output
    assert captured["event"].start.utcoffset() == timezone.utc.utcoffset(None)
