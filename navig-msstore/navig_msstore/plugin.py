"""The msstore plugin inside navig's daemon — the hourly Store sales check.

``register()`` is the ``navig.plugins`` entry point navig's gateway calls at boot. It
hooks ``gateway:register_routes`` and, from there, mounts three deck routes and starts
the sales poller on the gateway's own event loop — the only place a notification
reaches the desktop app live (a ``dispatch`` from a separate CLI process only lands in
the feed). Without navig nothing here runs, and the CLI verbs work as before.

    GET  /api/deck/msstore/sales         → poller status (enabled, last check, last error)
    POST /api/deck/msstore/sales/check   → check now (the same check the poller runs)
    POST /api/deck/msstore/sales/test    → send a pretend sale, to hear the ring
    POST /api/deck/msstore/sales/digest  → send the daily summary now (does not count as today's)
    GET  /api/deck/msstore/sales/history → the Store sales app: daily chart, per-app totals,
                                           past summaries and the watcher's status (?days=7-400)
    POST /api/deck/msstore/sales/history/backfill → fetch older days into the history ({"days": 90})

It also registers the ``sales`` module (the "Store sales" desktop app, Money category).

Settings (``navig config set plugins.msstore.<key> <value>``):
    sales_enabled       true   — set false to stop checking
    sales_interval_min  60     — minutes between checks (at least 15)
    sales_digest        true   — once a day: yesterday's sales + the last 7 days
    sales_digest_hour   9      — local hour after which the day's summary goes out (0-23)
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import asdict
from typing import Any

_log = logging.getLogger(__name__)
_REGISTERED = False

# The poller's live status — read by the status route, written only by the loop.
STATUS: dict[str, Any] = {
    "enabled": False,
    "running": False,
    "last_check": None,
    "last_error": None,
    "last_new": 0,
    "last_digest": None,
    # Consecutive failed checks, when the run of failures began, and whether the
    # owner was told (one alert per outage, one "working again" when it ends).
    "failures": 0,
    "failing_since": None,
    "alerted": False,
    # The history backfill: {"running", "days", "rows", "error", "finished"} or None.
    "backfill": None,
}
# Failed checks in a row before the owner is told (3 = three hours at the default pace).
FAIL_ALERT_AFTER = 3
# The window the last successful check read, and the local date it was read on: the
# digest is built from it, so it costs no extra Partner Center calls.
_window: tuple[dt.date, list] | None = None
_task: asyncio.Task | None = None
# The running history backfill, held so it is never garbage-collected mid-run.
_backfill_task: asyncio.Task | None = None
_lock: asyncio.Lock | None = None
# Wait after boot before the first check, so it never competes with startup.
FIRST_CHECK_DELAY_S = 90
MIN_INTERVAL_MIN = 15


def _setting(key: str, default: Any) -> Any:
    try:
        from navig_sdk.settings import get

        return get("msstore", key, default)
    except Exception:  # noqa: BLE001 — an unreadable config means "defaults", not a crash
        return default


def sales_enabled() -> bool:
    from navig_sdk.host import coerce_bool

    return coerce_bool(_setting("sales_enabled", True), default=True)


def interval_seconds() -> int:
    try:
        minutes = int(_setting("sales_interval_min", 60))
    except (TypeError, ValueError):
        minutes = 60
    return max(MIN_INTERVAL_MIN, minutes) * 60


def digest_enabled() -> bool:
    from navig_sdk.host import coerce_bool

    return coerce_bool(_setting("sales_digest", True), default=True)


def digest_hour() -> int:
    try:
        hour = int(_setting("sales_digest_hour", 9))
    except (TypeError, ValueError):
        return 9
    return hour if 0 <= hour <= 23 else 9


async def _notify(new: list, *, test: bool = False) -> None:
    """One notification per check, however many sales it found."""
    from navig.notify import dispatch

    from navig_msstore.sales import describe

    title, body = describe(new)
    total = round(sum(s.usd for s in new), 2)
    await dispatch(
        "sale",
        title,
        body,
        data={
            "source": "msstore",
            "test": test,
            "copies": sum(s.quantity for s in new),
            "total_usd": total,
            "sales": [
                {"app": s.app_name, "product": s.product_name, "date": s.date, "quantity": s.quantity, "usd": s.usd}
                for s in new
            ],
        },
    )


async def run_check() -> dict[str, Any]:
    """Run one check now; returns a JSON-safe summary. Never two at once."""
    global _lock, _window
    if _lock is None:
        _lock = asyncio.Lock()
    if _lock.locked():
        return {"skipped": "a check is already running"}
    async with _lock:
        from navig_msstore.addons import _client
        from navig_msstore.sales import check

        STATUS["running"] = True
        try:
            # Token + ~2 paced HTTP calls per app: seconds to minutes of blocking I/O.
            token = await asyncio.to_thread(_client, None)
            result = await asyncio.to_thread(check, token)
        except Exception as exc:  # noqa: BLE001 — reported in status, retried next interval
            STATUS["last_error"] = str(exc)[:300]
            _log.warning("msstore sales check failed: %s", exc)
            await _record_failure(exc)
            return {"error": STATUS["last_error"]}
        finally:
            STATUS["running"] = False
        STATUS.update(last_check=result["checked"], last_error=None, last_new=len(result["new"]))
        await _record_success()
        _window = (dt.date.today(), list(result.get("sales") or []))
        if result["new"]:
            await _notify(result["new"])
        return {
            "checked": result["checked"],
            "apps": result["apps"],
            "baseline": result["baseline"],
            "new": [asdict(s) for s in result["new"]],
        }


# ── When the check itself keeps failing ───────────────────────────────────────


def classify_failure(exc: BaseException) -> tuple[str, str]:
    """``(kind, plain reason)`` for a failed check — never the raw response body.

    kinds: ``credentials`` (Microsoft refused the app's sign-in: an expired or wrong
    secret), ``access`` (signed in, but the API said no), ``missing`` (no credentials
    configured), ``network`` (Microsoft could not be reached), ``api`` (anything else).
    """
    import re
    import urllib.error

    from navig_msstore.addons import DevCenterError

    if isinstance(exc, DevCenterError):
        code = re.search(r"AADSTS\d+", exc.body or "")
        suffix = f" ({code.group(0)})" if code else ""
        if exc.url == "token":
            return "credentials", f"Microsoft refused the Store sign-in{suffix}"
        if exc.status in (401, 403):
            return "access", f"Partner Center refused access (HTTP {exc.status})"
        return "api", f"Partner Center answered HTTP {exc.status}"
    if "Missing Store credentials" in str(exc):
        return "missing", "no Microsoft Store credentials are configured"
    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError)):
        return "network", "Microsoft could not be reached"
    return "api", type(exc).__name__


_FIX = {
    "credentials": "Renew the app secret in Partner Center / Entra ID, then run: navig mstore auth",
    "access": "Check the app still has access to the Partner Center account, then run: navig mstore auth",
    "missing": "Add them with: navig mstore auth",
    "network": "NAVIG keeps retrying every check; nothing to do if this is a short outage.",
    "api": "NAVIG keeps retrying every check.",
}


def _since_label(iso: str | None) -> str:
    try:
        return dt.datetime.fromisoformat(iso).astimezone().strftime("%a %H:%M") if iso else "?"
    except ValueError:
        return "?"


async def _alert(title: str, body: str, data: dict[str, Any]) -> None:
    try:
        from navig.notify import dispatch

        await dispatch("sales_check_failed", title, body, data={"source": "msstore", **data})
    except Exception as exc:  # noqa: BLE001 — an alert must never break the check loop
        _log.warning("msstore: could not send the check alert: %s", exc)


async def _record_failure(exc: BaseException) -> None:
    STATUS["failures"] = int(STATUS.get("failures") or 0) + 1
    if not STATUS.get("failing_since"):
        STATUS["failing_since"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    kind, reason = classify_failure(exc)
    STATUS["failure_kind"] = kind
    if STATUS["failures"] < FAIL_ALERT_AFTER or STATUS.get("alerted"):
        return
    STATUS["alerted"] = True
    body = (
        f"{STATUS['failures']} checks in a row failed (since {_since_label(STATUS['failing_since'])}): {reason}.\n"
        "The sale ring and the daily summary are paused until a check succeeds.\n"
        f"{_FIX[kind]}"
    )
    await _alert("⚠️ Store sales check is failing", body, {"kind": kind, "failures": STATUS["failures"]})


async def _record_success() -> None:
    was_alerted, since = STATUS.get("alerted"), STATUS.get("failing_since")
    STATUS.update(failures=0, failing_since=None, alerted=False, failure_kind=None)
    if not was_alerted:
        return
    from navig_msstore.sales import WINDOW_DAYS

    try:
        days = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(since)).days if since else 0
    except ValueError:
        days = 0
    if days < WINDOW_DAYS:
        tail = f"Each check re-reads the last {WINDOW_DAYS} days, so sales made during the outage still ring."
    else:
        tail = (
            f"The outage lasted {days} days; only the last {WINDOW_DAYS} were re-read. "
            "See the rest with: navig mstore sales --days 30"
        )
    await _alert("✅ Store sales check is working again", tail,
                 {"kind": "recovered", "outage_days": days, "tidy": "status"})


async def send_digest(
    sales: list, today: dt.date | None = None, known: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Send the daily summary built from *sales* (one check's window), as of *today*.

    *known* is what earlier summaries reported per day, so a day that has grown
    since is listed as "arrived late"."""
    from navig.notify import dispatch

    from navig_msstore.sales import digest

    title, body, data = digest(sales, today=today, known=known)
    await dispatch("sales_digest", title, body, data=data)
    return {"title": title, "body": body, "data": data}


async def maybe_send_digest(now: dt.datetime | None = None) -> bool:
    """Send today's summary if it is due and today's data is fresh. True when sent.

    Only from a check that succeeded today: a summary built from yesterday's read
    would report a day that was not over yet as if it were complete.
    """
    from navig_msstore.sales import digest_due, load_digest_state, mark_digest_sent

    if not digest_enabled() or _window is None:
        return False
    now = now or dt.datetime.now()
    read_on, sales = _window
    if read_on != now.date():
        return False
    try:
        state = await asyncio.to_thread(load_digest_state)
    except Exception as exc:  # noqa: BLE001 — unreadable state: skip, never send twice
        _log.warning("msstore digest state unreadable, skipping this round: %s", exc)
        return False
    last = state.get("last_sent") if isinstance(state.get("last_sent"), str) else None
    if not digest_due(now, digest_hour(), last):
        return False
    sent = await send_digest(sales, now.date(), state.get("known") or {})
    data = sent["data"]
    entry = {
        "date": data["date"],
        "sent_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "title": sent["title"],
        "body": sent["body"],
        "copies": data["copies"],
        "total_usd": data["total_usd"],
        "week_copies": data["week_copies"],
        "week_usd": data["week_usd"],
        "late_copies": sum(int(i["copies"]) for i in data.get("late") or []),
    }
    await asyncio.to_thread(
        lambda: mark_digest_sent(now.date(), known=data["known"], previous=state, entry=entry)
    )
    STATUS["last_digest"] = now.date().isoformat()
    return True


# ── The history behind the Store sales app ────────────────────────────────────


async def run_backfill(days: int) -> dict[str, Any]:
    """Fetch *days* of history into the history file. Shares the check's lock, so the
    two never hammer the rate-limited API at once."""
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    if _lock.locked():
        return {"skipped": "a check is running — try again in a minute"}
    async with _lock:
        from navig_msstore.addons import _client
        from navig_msstore.sales import backfill_history

        STATUS["backfill"] = {"running": True, "days": days, "rows": None, "error": None, "finished": None}
        try:
            token = await asyncio.to_thread(_client, None)
            rows = await asyncio.to_thread(lambda: backfill_history(token, days=days))
        except Exception as exc:  # noqa: BLE001 — reported in status; the app offers a retry
            kind, reason = classify_failure(exc)
            STATUS["backfill"] = {**STATUS["backfill"], "running": False, "error": reason}
            _log.warning("msstore history backfill failed (%s): %s", kind, exc)
            return {"error": reason}
        done = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        STATUS["backfill"] = {**STATUS["backfill"], "running": False, "rows": rows, "finished": done}
        return {"rows": rows}


async def maybe_backfill() -> bool:
    """Once, fill the history with the last 90 days, so the app's chart starts full
    instead of growing a day at a time. True when it ran."""
    from navig_msstore.sales import BACKFILL_DAYS, load_history

    if STATUS.get("backfill") is not None:  # tried in this process (ran, or failed)
        return False
    try:
        history = await asyncio.to_thread(load_history)
    except Exception:  # noqa: BLE001 — unreadable history: never overwrite it
        return False
    if int(history.get("backfilled_days") or 0) >= BACKFILL_DAYS:
        return False
    result = await run_backfill(BACKFILL_DAYS)
    return "rows" in result


async def _loop() -> None:
    await asyncio.sleep(FIRST_CHECK_DELAY_S)
    while True:
        if sales_enabled():
            await run_check()
            try:
                await maybe_send_digest()
            except Exception as exc:  # noqa: BLE001 — the summary must never stop the ring
                _log.warning("msstore daily digest failed: %s", exc)
            if not STATUS.get("failures"):
                try:
                    await maybe_backfill()
                except Exception as exc:  # noqa: BLE001
                    _log.warning("msstore history backfill failed: %s", exc)
        await asyncio.sleep(interval_seconds())


def _has_credentials() -> bool:
    try:
        from navig_msstore import _FIELDS, _resolve_value

        return all(_resolve_value(name)[0] for name in _FIELDS)
    except Exception:  # noqa: BLE001
        return False


async def _on_startup(_app: Any) -> None:
    global _task
    STATUS["enabled"] = sales_enabled()
    if not STATUS["enabled"]:
        _log.info("msstore sales check off (plugins.msstore.sales_enabled)")
        return
    if not await asyncio.to_thread(_has_credentials):
        STATUS["enabled"] = False
        STATUS["last_error"] = "no Microsoft Store credentials — run: navig mstore auth"
        return
    try:
        from navig.core.background import spawn

        _task = spawn(_loop(), name="msstore-sales")
    except Exception:  # noqa: BLE001 — older core without the helper
        _task = asyncio.get_running_loop().create_task(_loop(), name="msstore-sales")


async def _on_cleanup(_app: Any) -> None:
    if _task is not None and not _task.done():
        _task.cancel()


# ── Deck routes ───────────────────────────────────────────────────────────────


def _json(data: Any, status: int = 200):
    from aiohttp import web

    ok = status < 400
    return web.json_response({"ok": ok, "data": data} if ok else {"ok": False, "error": data}, status=status)


async def handle_status(_request: Any):
    return _json(
        {
            **STATUS,
            "interval_min": interval_seconds() // 60,
            "digest_enabled": digest_enabled(),
            "digest_hour": digest_hour(),
        }
    )


async def handle_check(_request: Any):
    result = await run_check()
    return _json(result["error"], 502) if "error" in result else _json(result)


async def handle_test(_request: Any):
    """A pretend sale through the real notification path — how you hear the ring."""
    from navig_msstore.sales import Sale

    sample = Sale("test", "Your app", "test", "Test sale", "today", 1, 0.99)
    await _notify([sample], test=True)
    return _json({"sent": True})


async def handle_digest(_request: Any):
    """Send the daily summary now — to see it without waiting for the morning.

    Uses today's data when there is some, otherwise checks first. It does not count as
    the day's summary, so the scheduled one still goes out."""
    if _window is None or _window[0] != dt.date.today():
        result = await run_check()
        if "error" in result:
            return _json(result["error"], 502)
        if "skipped" in result:
            return _json(result["skipped"], 409)
    if _window is None:
        return _json("no sales data yet", 503)
    from navig_msstore.sales import load_digest_state

    try:
        known = (await asyncio.to_thread(load_digest_state)).get("known") or {}
    except Exception:  # noqa: BLE001 — a preview without "late" lines beats no preview
        known = {}
    sent = await send_digest(_window[1], known=known)
    return _json({"title": sent["title"], "body": sent["body"]})


def _days_param(request: Any, default: int = 30) -> int:
    try:
        days = int(request.query.get("days", default))
    except (TypeError, ValueError, AttributeError):
        days = default
    return max(7, min(400, days))


async def handle_history(request: Any):
    """Everything the Store sales app shows, in one call."""
    from navig_msstore.sales import history_series, load_digest_state, load_history

    days = _days_param(request)
    try:
        history = await asyncio.to_thread(load_history)
    except Exception as exc:  # noqa: BLE001 — locked file: say so instead of an empty chart
        return _json(f"sales history unreadable right now: {exc}", 503)
    try:
        digests = (await asyncio.to_thread(load_digest_state)).get("history") or []
    except Exception:  # noqa: BLE001 — the chart is still worth showing
        digests = []
    series = history_series(history, days=days)
    status = {
        **{k: v for k, v in STATUS.items() if k != "last_error"},
        # The raw error can carry Microsoft's response text; the app gets the plain reason.
        "last_error": None if not STATUS.get("last_error") else _plain_error(),
        "interval_min": interval_seconds() // 60,
        "digest_enabled": digest_enabled(),
        "digest_hour": digest_hour(),
    }
    return _json({**series, "digests": list(reversed(digests))[:30], "status": status})


def _plain_error() -> str:
    kind = STATUS.get("failure_kind")
    labels = {
        "credentials": "Microsoft refused the Store sign-in — run: navig mstore auth",
        "access": "Partner Center refused access — run: navig mstore auth",
        "missing": "No Microsoft Store credentials — run: navig mstore auth",
        "network": "Microsoft could not be reached",
    }
    return labels.get(kind or "", "The last check failed")


async def handle_backfill(request: Any):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — no body = the default range
        body = {}
    try:
        days = max(7, min(365, int((body or {}).get("days", 90))))
    except (TypeError, ValueError):
        days = 90
    if _lock is not None and _lock.locked():
        return _json("a check is running — try again in a minute", 409)
    # Two paced calls per app: it takes minutes, so it runs in the background and the
    # app polls the status.
    global _backfill_task
    try:
        from navig.core.background import spawn

        _backfill_task = spawn(run_backfill(days), name="msstore-backfill")
    except Exception:  # noqa: BLE001 — older core without the helper
        _backfill_task = asyncio.get_running_loop().create_task(run_backfill(days), name="msstore-backfill")
    return _json({"started": True, "days": days}, 202)


# Store sales is money in: surfaces show it as Finance's "Store sales" section while
# Finance can host it (present, enabled, unlocked), and as its own tile otherwise —
# Finance is a Harbor app and this one is free, so it never moves behind the paywall.
FOLD = {"merged_into": "finance", "merged_section": "Store"}


def _fold_kwargs(model: Any) -> dict[str, str]:
    """The fold fields this core's ModuleDef knows. The plugin runs on any core, and a
    core older than the field would raise TypeError on an unknown kwarg — dropping the
    module from the catalog entirely. Surfaces fall back to their own table then."""
    from dataclasses import fields

    known = {f.name for f in fields(model)}
    return {k: v for k, v in FOLD.items() if k in known}


def _sales_module_def():
    from navig.modules.registry import ModuleDef, ModuleKind

    return ModuleDef(
        id="sales",
        label="Store sales",
        description="Microsoft Store sales by day, per app, and the daily summaries.",
        kind=ModuleKind.APP,
        category="operate",
        icon="chart-column",
        capability=None,  # free: it reads the operator's own Partner Center data
        surfaces=["os-tile:sales"],
        requires=["gateway"],
        default_enabled=True,
        source="plugin",
        app_category="Money",
        scope="brain",
        **_fold_kwargs(ModuleDef),
    )


def _register_deck_routes(event: Any) -> None:
    app = (getattr(event, "context", None) or {}).get("app")
    if app is None:
        return
    app.router.add_get("/api/deck/msstore/sales", handle_status)
    app.router.add_post("/api/deck/msstore/sales/check", handle_check)
    app.router.add_post("/api/deck/msstore/sales/test", handle_test)
    app.router.add_post("/api/deck/msstore/sales/digest", handle_digest)
    app.router.add_get("/api/deck/msstore/sales/history", handle_history)
    app.router.add_post("/api/deck/msstore/sales/history/backfill", handle_backfill)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)


def register() -> None:
    """Entry point (``navig.plugins``): wire the routes + poller into the gateway."""
    global _REGISTERED
    if _REGISTERED:
        return
    try:
        from navig.core.hooks import register_hook
    except Exception as exc:  # noqa: BLE001 — core too old / running standalone
        _log.debug("msstore: hook bus unavailable (%s)", exc)
        return
    register_hook("gateway:register_routes", _register_deck_routes)
    try:
        from navig.modules.registry import register_module

        register_module(_sales_module_def())
    except Exception as exc:  # noqa: BLE001 — the app tile is optional; the ring is not
        _log.warning("msstore: module registry unavailable (%s)", exc)
    try:
        # The Projects hub's Links card. A core that predates the hub has no
        # navig.projects (plugins pin an older floor) — the card is simply absent there.
        from navig.projects.links import register_link_provider

        from navig_msstore.project_links import store_card

        register_link_provider("store", store_card)
    except ImportError as exc:
        _log.debug("msstore: projects hub unavailable (%s)", exc)
    _REGISTERED = True
