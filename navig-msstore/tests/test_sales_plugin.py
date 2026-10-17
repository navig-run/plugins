"""The daemon side of the sale ring: routes + poller hooks, and the notification it sends."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

pytest.importorskip("aiohttp")
pytest.importorskip("navig")

from aiohttp import web

from navig_msstore import plugin


def test_routes_and_lifecycle_hooks_are_wired():
    app = web.Application()
    plugin._register_deck_routes(SimpleNamespace(context={"app": app}))
    paths = {(r.method, r.resource.canonical) for r in app.router.routes()}
    assert ("GET", "/api/deck/msstore/sales") in paths
    assert ("POST", "/api/deck/msstore/sales/check") in paths
    assert ("POST", "/api/deck/msstore/sales/test") in paths
    assert ("POST", "/api/deck/msstore/sales/digest") in paths
    assert plugin._on_startup in app.on_startup and plugin._on_cleanup in app.on_cleanup


def test_sale_is_a_registered_notification_type():
    from navig.notify.types import TYPE_KEYS

    # An unregistered type only reaches the deck feed (with a warning) — never Telegram.
    assert "sale" in TYPE_KEYS


@pytest.mark.asyncio
async def test_the_test_route_sends_a_sale_notification(monkeypatch):
    sent = []

    async def fake_dispatch(type_key, title, body="", **kw):
        sent.append((type_key, title, body, kw))
        return {}

    import navig.notify

    monkeypatch.setattr(navig.notify, "dispatch", fake_dispatch)
    resp = await plugin.handle_test(None)
    assert resp.status == 200 and json.loads(resp.body)["data"] == {"sent": True}
    (type_key, title, _body, kw), = sent
    assert type_key == "sale" and title.startswith("💰 New sale")
    assert kw["data"]["test"] is True and kw["data"]["copies"] == 1 and kw["data"]["source"] == "msstore"
    assert set(kw) == {"data"}  # dispatch() takes no other keyword the router doesn't define


@pytest.mark.asyncio
async def test_startup_without_credentials_does_not_poll(monkeypatch):
    monkeypatch.setattr(plugin, "_has_credentials", lambda: False)
    monkeypatch.setattr(plugin, "sales_enabled", lambda: True)
    plugin._task = None
    await plugin._on_startup(None)
    assert plugin._task is None and plugin.STATUS["enabled"] is False
    assert "navig mstore auth" in plugin.STATUS["last_error"]


def test_interval_has_a_floor_and_survives_junk(monkeypatch):
    monkeypatch.setattr(plugin, "_setting", lambda key, default: "5")
    assert plugin.interval_seconds() == plugin.MIN_INTERVAL_MIN * 60
    monkeypatch.setattr(plugin, "_setting", lambda key, default: "soon")
    assert plugin.interval_seconds() == 60 * 60


# ── The daily digest ──────────────────────────────────────────────────────────

import datetime as dt  # noqa: E402

from navig_msstore import sales as S  # noqa: E402

SALE = S.Sale("9M", "Matrix", "pro", "Pro", "2026-10-03", 1, 6.5)


@pytest.fixture
def digest_env(monkeypatch, tmp_path):
    sent: list = []

    async def fake_dispatch(type_key, title, body="", **kw):
        sent.append((type_key, title, body, kw))
        return {}

    import navig.notify

    path = tmp_path / "digest_state.json"
    monkeypatch.setattr(navig.notify, "dispatch", fake_dispatch)
    monkeypatch.setattr(S, "digest_state_path", lambda: path)
    monkeypatch.setattr(plugin, "_setting", lambda key, default: default)
    monkeypatch.setattr(plugin, "_window", (dt.date(2026, 10, 4), [SALE]))
    return SimpleNamespace(sent=sent, path=path)


def test_the_digest_is_a_registered_type_that_never_rings():
    from navig.notify.types import TYPE_KEYS

    assert "sales_digest" in TYPE_KEYS  # unregistered would never reach Telegram
    assert "sales_digest" != "sale"  # NAVIG OS rings only for "sale"


@pytest.mark.asyncio
async def test_the_digest_goes_out_once_a_day_after_the_hour(digest_env):
    early = dt.datetime(2026, 10, 4, 8, 30)
    assert await plugin.maybe_send_digest(early) is False and digest_env.sent == []

    assert await plugin.maybe_send_digest(early.replace(hour=9, minute=5)) is True
    (type_key, title, body, kw), = digest_env.sent
    assert type_key == "sales_digest" and "1 sale" in title and set(kw) == {"data"}
    saved = json.loads(digest_env.path.read_text(encoding="utf-8"))
    assert saved["last_sent"] == "2026-10-04"
    assert saved["known"]["2026-10-03"] == {"copies": 1, "usd": 6.5}  # what it reported, for "late" next time

    # The next hourly check, and a restart (fresh process state, same file): nothing.
    assert await plugin.maybe_send_digest(early.replace(hour=10)) is False
    assert len(digest_env.sent) == 1


@pytest.mark.asyncio
async def test_no_digest_from_a_stale_read_or_when_switched_off(digest_env, monkeypatch):
    tomorrow_morning = dt.datetime(2026, 10, 5, 9, 30)
    assert await plugin.maybe_send_digest(tomorrow_morning) is False  # window read yesterday
    monkeypatch.setattr(plugin, "_setting", lambda key, default: "false" if key == "sales_digest" else default)
    assert await plugin.maybe_send_digest(dt.datetime(2026, 10, 4, 9, 30)) is False
    assert digest_env.sent == []


@pytest.mark.asyncio
async def test_an_unreadable_digest_state_skips_rather_than_sending_twice(digest_env, monkeypatch):
    def locked(*_a, **_k):
        raise OSError("locked")

    monkeypatch.setattr(S, "load_digest_state", locked)
    assert await plugin.maybe_send_digest(dt.datetime(2026, 10, 4, 9, 30)) is False
    assert digest_env.sent == []


@pytest.mark.asyncio
async def test_the_digest_route_sends_now_without_using_up_the_day(digest_env, monkeypatch):
    monkeypatch.setattr(plugin, "_window", (dt.date.today(), [SALE]))
    resp = await plugin.handle_digest(None)
    assert resp.status == 200 and json.loads(resp.body)["data"]["title"].startswith("📊 Store sales")
    assert len(digest_env.sent) == 1 and not digest_env.path.exists()


def test_digest_hour_survives_junk(monkeypatch):
    monkeypatch.setattr(plugin, "_setting", lambda key, default: "25")
    assert plugin.digest_hour() == 9
    monkeypatch.setattr(plugin, "_setting", lambda key, default: "7")
    assert plugin.digest_hour() == 7


@pytest.mark.asyncio
async def test_a_day_that_grew_after_its_summary_is_reported_late(digest_env, monkeypatch):
    # Thursday's summary said "no sales" for Wed 7 Oct …
    monkeypatch.setattr(plugin, "_window", (dt.date(2026, 10, 8), []))
    assert await plugin.maybe_send_digest(dt.datetime(2026, 10, 8, 9, 30)) is True
    assert "no sales" in digest_env.sent[-1][1]

    # … then Microsoft filled Wednesday in. Friday's summary says so.
    late = S.Sale("9M", "Matrix", "pro", "Pro", "2026-10-07", 4, 26.0)
    monkeypatch.setattr(plugin, "_window", (dt.date(2026, 10, 9), [late]))
    assert await plugin.maybe_send_digest(dt.datetime(2026, 10, 9, 9, 30)) is True
    _type, _title, body, kw = digest_env.sent[-1]
    assert "Arrived late: 4 sales · $26.00 for Wed 7 Oct" in body
    assert kw["data"]["late"] == [{"date": "2026-10-07", "copies": 4, "usd": 26.0}]


# ── When the check itself keeps failing ───────────────────────────────────────


@pytest.fixture
def alert_env(monkeypatch):
    sent: list = []

    async def fake_dispatch(type_key, title, body="", **kw):
        sent.append((type_key, title, body, kw))
        return {}

    import navig.notify

    monkeypatch.setattr(navig.notify, "dispatch", fake_dispatch)
    monkeypatch.setattr(plugin, "STATUS", {**plugin.STATUS, "failures": 0, "failing_since": None, "alerted": False})
    return sent


def _token_refused():
    from navig_msstore.addons import DevCenterError

    body = '{"error":"invalid_client","error_description":"AADSTS7000222: The provided client secret keys are expired."}'
    return DevCenterError(401, body, "token")


@pytest.mark.asyncio
async def test_three_failures_in_a_row_alert_once_then_recovery_says_so(alert_env):
    for _ in range(2):
        await plugin._record_failure(_token_refused())
    assert alert_env == []  # one or two misses are routine

    await plugin._record_failure(_token_refused())
    await plugin._record_failure(_token_refused())  # a fourth: still one alert
    (type_key, title, body, kw), = alert_env
    assert type_key == "sales_check_failed" and "failing" in title
    assert "Microsoft refused the Store sign-in (AADSTS7000222)" in body and "navig mstore auth" in body
    assert "client secret keys" not in body  # the raw response never reaches Telegram
    assert kw["data"]["kind"] == "credentials" and kw["data"]["failures"] == 3

    await plugin._record_success()
    assert alert_env[-1][1].startswith("✅") and "still ring" in alert_env[-1][2]
    assert plugin.STATUS["failures"] == 0 and plugin.STATUS["alerted"] is False
    await plugin._record_success()
    assert len(alert_env) == 2  # no "working again" without an outage


def test_failures_are_classified_without_leaking_the_body():
    import urllib.error

    from navig_msstore.addons import DevCenterError

    assert plugin.classify_failure(_token_refused())[0] == "credentials"
    assert plugin.classify_failure(DevCenterError(403, "nope", "https://x"))[0] == "access"
    assert plugin.classify_failure(DevCenterError(500, "boom", "https://x")) == ("api", "Partner Center answered HTTP 500")
    assert plugin.classify_failure(urllib.error.URLError("dns"))[0] == "network"
    assert plugin.classify_failure(ValueError("Missing Store credentials: tenant_id"))[0] == "missing"


@pytest.mark.asyncio
async def test_a_failing_check_counts_through_run_check(alert_env, monkeypatch):
    import navig_msstore.addons as addons

    def refused(_creds):
        raise _token_refused()

    monkeypatch.setattr(addons, "_client", refused)
    monkeypatch.setattr(plugin, "_lock", None)
    for _ in range(plugin.FAIL_ALERT_AFTER):
        assert "error" in await plugin.run_check()
    assert plugin.STATUS["failures"] == plugin.FAIL_ALERT_AFTER and len(alert_env) == 1


def test_the_alert_is_a_registered_type():
    from navig.notify.types import TYPE_KEYS

    assert "sales_check_failed" in TYPE_KEYS


# ── The Store sales app's routes ──────────────────────────────────────────────


def test_the_store_sales_app_is_a_free_money_tile():
    m = plugin._sales_module_def()
    assert m.id == "sales" and "os-tile:sales" in m.surfaces
    assert m.app_category == "Money" and m.capability is None and m.icon == "chart-column"


def test_history_routes_are_wired():
    app = web.Application()
    plugin._register_deck_routes(SimpleNamespace(context={"app": app}))
    paths = {(r.method, r.resource.canonical) for r in app.router.routes()}
    assert ("GET", "/api/deck/msstore/sales/history") in paths
    assert ("POST", "/api/deck/msstore/sales/history/backfill") in paths


@pytest.mark.asyncio
async def test_history_route_returns_the_chart_summaries_and_a_plain_status(monkeypatch, tmp_path):
    hist, digests = tmp_path / "h.json", tmp_path / "d.json"
    S.save_history(S.merge_history({}, [SALE]), hist)
    S.mark_digest_sent(dt.date(2026, 10, 4), digests, entry={"date": "2026-10-03", "title": "📊 one"})
    monkeypatch.setattr(S, "history_path", lambda: hist)
    monkeypatch.setattr(S, "digest_state_path", lambda: digests)
    monkeypatch.setattr(plugin, "_setting", lambda key, default: default)
    monkeypatch.setattr(plugin, "STATUS", {**plugin.STATUS, "last_error": "HTTP 401 for token\n{secret body}", "failure_kind": "credentials"})
    resp = await plugin.handle_history(SimpleNamespace(query={"days": "400"}))
    data = json.loads(resp.body)["data"]
    assert len(data["days"]) == 400 and data["digests"][0]["title"] == "📊 one"
    assert data["status"]["last_error"].startswith("Microsoft refused the Store sign-in")
    assert "secret body" not in resp.body.decode()


@pytest.mark.asyncio
async def test_backfill_route_runs_in_the_background_and_refuses_during_a_check(monkeypatch):
    started = []

    async def fake_backfill(days):
        started.append(days)
        return {"rows": 0}

    monkeypatch.setattr(plugin, "run_backfill", fake_backfill)
    monkeypatch.setattr(plugin, "_lock", None)

    class Req:
        async def json(self):
            return {"days": 9999}

    resp = await plugin.handle_backfill(Req())
    assert resp.status == 202 and json.loads(resp.body)["data"] == {"started": True, "days": 365}
    import asyncio

    await asyncio.sleep(0)
    assert started == [365]

    lock = asyncio.Lock()
    await lock.acquire()
    monkeypatch.setattr(plugin, "_lock", lock)
    assert (await plugin.handle_backfill(Req())).status == 409
    lock.release()


@pytest.mark.asyncio
async def test_the_automatic_backfill_runs_once(monkeypatch, tmp_path):
    hist = tmp_path / "h.json"
    monkeypatch.setattr(S, "history_path", lambda: hist)
    monkeypatch.setattr(plugin, "STATUS", {**plugin.STATUS, "backfill": None})
    calls = []

    async def fake_run(days):
        calls.append(days)
        plugin.STATUS["backfill"] = {"running": False}
        return {"rows": 3}

    monkeypatch.setattr(plugin, "run_backfill", fake_run)
    assert await plugin.maybe_backfill() is True
    assert await plugin.maybe_backfill() is False  # once per process
    assert calls == [S.BACKFILL_DAYS]

    S.save_history({"rows": {}, "backfilled_days": 90}, hist)
    monkeypatch.setattr(plugin, "STATUS", {**plugin.STATUS, "backfill": None})
    assert await plugin.maybe_backfill() is False  # already done on disk


# ── The fold into Finance, declared by the plugin itself ──────────────────────


def test_store_sales_declares_its_fold_into_finance():
    """Surfaces learn the fold from the catalog: a visible app with merged_into +
    merged_section is Finance's "Store sales" section while Finance can host it, and
    keeps its own tile (hidden=False, os-tile) for a Free user whose Finance is locked."""
    m = plugin._sales_module_def()
    assert (m.merged_into, m.merged_section) == ("finance", "Store")
    assert m.hidden is False and "os-tile:sales" in m.surfaces and m.capability is None
    row = m.to_dict(capabilities=[], enabled=True)
    assert row["merged_into"] == "finance" and row["merged_section"] == "Store"


def test_an_older_core_without_the_fold_fields_still_registers(monkeypatch):
    """A core that predates merged_section (or merged_into) must not see an unknown
    kwarg: that TypeError would drop Store sales from the catalog altogether."""
    from dataclasses import dataclass, field

    import navig.modules.registry as registry

    @dataclass
    class OldModuleDef:  # the pre-fold shape: no merged_into, no merged_section
        id: str
        label: str
        description: str
        kind: object = None
        category: str = "operate"
        icon: str = "box"
        capability: object = None
        surfaces: list = field(default_factory=list)
        requires: list = field(default_factory=list)
        default_enabled: bool = True
        source: str = "builtin"
        app_category: object = None
        scope: str = "brain"

    monkeypatch.setattr(registry, "ModuleDef", OldModuleDef)
    m = plugin._sales_module_def()
    assert isinstance(m, OldModuleDef) and m.id == "sales" and "os-tile:sales" in m.surfaces
