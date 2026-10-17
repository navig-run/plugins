"""Agent Monitor deck routes — the run ledger's ingestion and read API.

Mounted by ``deck_routes.register`` next to the Observer routes, behind the same
``agentctl`` module gate. The contract fixture is
``tests/fixtures/agents-contract.json`` (``runs`` section).

  POST   /api/deck/agents/events              → {ok, results: [...]}   one event or {events: [...≤50]}
  GET    /api/deck/agents/runs[?project=&state=&kind=&active=1&limit=] → {runs: [...], generated_at}
  GET    /api/deck/agents/runs/summary        → {summary: {...counts, attention}, generated_at}
  GET    /api/deck/agents/runs/{id}[?log=1]   → {run, events: [...], log: [...redacted tail]}
  POST   /api/deck/agents/runs/{id}/ack       → {ok, acked}   clears a failure from the indicator

State transitions are decided by ``RunsMixin.apply_event``; this module only
validates, persists, and fans changes out (``publish.publish``). A background
sweeper drains the offline spool and flags stalled / vanished runs.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any

try:
    from aiohttp import web
except ImportError:  # pragma: no cover — gateway always has aiohttp
    web = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

BASE = "/api/deck/agents"
SWEEP_INTERVAL_S = 30
MAX_EVENTS_PER_POST = 50

_STR_FIELDS = {
    "event_id": 160, "run_id": 160, "type": 20, "kind": 20, "session_id": 200, "parent_run_id": 160,
    "project": 120, "worktree": 200, "node": 80, "label": 160, "command": 2000, "detail": 2000,
    "log_path": 400, "state": 20, "phase": 20,
}


def _json(data: dict[str, Any], status: int = 200):
    return web.json_response(data, status=status)


def _reject(msg: str):
    logger.warning("agentctl monitor request rejected: %s", msg)
    return _json({"error": msg}, status=400)


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _local_node() -> str:
    return socket.gethostname()


def clean_event(raw: Any) -> tuple[dict[str, Any] | None, str]:
    """Whitelist + type-check one producer event. Returns ``(event, "")`` or ``(None, reason)``."""
    from navig_agentctl.runs import is_valid_id

    if not isinstance(raw, dict):
        return None, "event must be a JSON object"
    ev: dict[str, Any] = {}
    for key, limit in _STR_FIELDS.items():
        val = raw.get(key)
        if val is None:
            continue
        if not isinstance(val, str) or len(val) > limit:
            return None, f"{key} must be a string of at most {limit} chars"
        ev[key] = val
    if not is_valid_id(ev.get("run_id")):
        return None, "run_id is required (non-empty string, ≤160 chars)"
    for key in ("pid", "exit_code", "stall_after_s"):
        if raw.get(key) is not None:
            if isinstance(raw[key], bool) or not isinstance(raw[key], int):
                return None, f"{key} must be an integer"
            ev[key] = raw[key]
    if raw.get("pid_create_time") is not None:
        if isinstance(raw["pid_create_time"], bool) or not isinstance(raw["pid_create_time"], (int, float)):
            return None, "pid_create_time must be a number"
        ev["pid_create_time"] = float(raw["pid_create_time"])
    for key in ("verified", "background"):
        if raw.get(key) is not None:
            if not isinstance(raw[key], bool):
                return None, f"{key} must be a boolean"
            ev[key] = raw[key]
    if raw.get("stage") is not None:
        st = raw["stage"]
        if not isinstance(st, dict) or not isinstance(st.get("name"), str) or len(st["name"]) > 160:
            return None, "stage must be {name: str, state?: str, detail?: str}"
        ev["stage"] = {k: str(st[k])[:300] for k in ("name", "state", "detail") if st.get(k) is not None}
    if raw.get("stages_planned") is not None:
        sp = raw["stages_planned"]
        if not isinstance(sp, list) or len(sp) > 60 or not all(isinstance(x, str) and len(x) <= 160 for x in sp):
            return None, "stages_planned must be a list of at most 60 short strings"
        ev["stages_planned"] = sp
    return ev, ""


async def ingest(app, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply already-cleaned events to the ledger and publish each change."""
    from navig_agentctl.publish import publish
    from navig_agentctl.registry import get_agentctl_store

    store = get_agentctl_store()
    results = []
    for ev in events:
        ev.setdefault("node", _local_node())
        res = await asyncio.to_thread(store.apply_event, ev)
        await publish(app, res)
        results.append(
            {"run_id": ev["run_id"], "duplicate": res["duplicate"],
             "state": res["run"]["state"] if res["run"] else None}
        )
    return results


async def handle_event(request):
    """POST — ingest producer events (Claude Code hook, ``agentctl run``, land adapter)."""
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 — non-JSON body
        return _reject("body must be a JSON object")
    raw_events = payload.get("events") if isinstance(payload, dict) and "events" in payload else [payload]
    if not isinstance(raw_events, list) or not raw_events or len(raw_events) > MAX_EVENTS_PER_POST:
        return _reject(f"send one event or {{events: [1..{MAX_EVENTS_PER_POST}]}}")
    cleaned = []
    for raw in raw_events:
        ev, why = clean_event(raw)
        if ev is None:
            return _reject(why)
        cleaned.append(ev)
    try:
        results = await ingest(request.app, cleaned)
    except ValueError as exc:
        return _reject(str(exc))
    except Exception as exc:  # noqa: BLE001 — store down is a 500, not a silent 200
        logger.warning("agentctl event ingest failed: %r", exc)
        return _json({"error": f"run store failed: {exc}"}, status=500)
    return _json({"ok": True, "results": results})


def _node_fill(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    here = _local_node()
    for r in runs:
        r["node"] = r["node"] or here
    return runs


async def handle_runs_list(request):
    from navig_agentctl.registry import get_agentctl_store

    q = request.query
    try:
        limit = int(q.get("limit") or 100)
    except ValueError:
        return _reject("limit must be an integer")
    active = (q.get("active") or "0").lower() in ("1", "true", "yes")
    try:
        store = get_agentctl_store()
        runs = await asyncio.to_thread(
            lambda: store.list_runs(
                project=q.get("project", ""), state=q.get("state", ""),
                kind=q.get("kind", ""), active_only=active, limit=limit,
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("agentctl runs list failed: %r", exc)
        return _json({"error": f"run store failed: {exc}"}, status=500)
    return _json({"runs": _node_fill(runs), "generated_at": _now()})


async def handle_runs_summary(request):
    from navig_agentctl.registry import get_agentctl_store

    try:
        summary = await asyncio.to_thread(get_agentctl_store().runs_summary)
    except Exception as exc:  # noqa: BLE001
        logger.warning("agentctl runs summary failed: %r", exc)
        return _json({"error": f"run store failed: {exc}"}, status=500)
    return _json({"summary": summary, "generated_at": _now()})


async def handle_run_detail(request):
    from navig_agentctl import logs
    from navig_agentctl.registry import get_agentctl_store

    rid = request.match_info.get("run_id", "")
    store = get_agentctl_store()
    want_log = (request.query.get("log") or "0").lower() in ("1", "true", "yes")

    def _load():
        raw = store.get_run_raw(rid)
        if raw is None:
            return None
        body: dict[str, Any] = {"run": _node_fill([store.get_run(rid)])[0], "events": store.run_events(rid)}
        if want_log:
            body["log"] = logs.tail(raw["log_path"])
        return body

    try:
        body = await asyncio.to_thread(_load)
    except Exception as exc:  # noqa: BLE001
        logger.warning("agentctl run detail failed: %r", exc)
        return _json({"error": f"run store failed: {exc}"}, status=500)
    if body is None:
        return _json({"error": "no such run"}, status=404)
    return _json(body)


async def handle_run_ack(request):
    from navig_agentctl.registry import get_agentctl_store

    rid = request.match_info.get("run_id", "")
    try:
        acked = await asyncio.to_thread(get_agentctl_store().ack_run, rid)
    except Exception as exc:  # noqa: BLE001
        return _json({"error": f"run store failed: {exc}"}, status=500)
    return _json({"ok": True, "acked": acked})


async def sweep_once(app) -> int:
    """One reaper pass: drain the offline spool, then flag stalls / vanished processes."""
    from navig_agentctl import spool
    from navig_agentctl.publish import publish
    from navig_agentctl.registry import get_agentctl_store

    store = get_agentctl_store()
    drained = 0
    for raw in await asyncio.to_thread(spool.drain):
        ev, why = clean_event(raw)
        if ev is None:
            logger.debug("agentctl spool: dropped event (%s)", why)
            continue
        try:
            await ingest(app, [ev])
            drained += 1
        except Exception:  # noqa: BLE001 — one bad event must not stall the drain
            logger.debug("agentctl spool: event rejected", exc_info=True)
    for item in await asyncio.to_thread(store.sweep_runs):
        await publish(app, {"run": item["run"], "transition": {"kind": item["kind"]}})
    return drained


async def _sweeper(app) -> None:
    while True:
        try:
            await sweep_once(app)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the reaper must outlive any single failure
            logger.warning("agentctl sweeper pass failed", exc_info=True)
        await asyncio.sleep(SWEEP_INTERVAL_S)


async def _sweeper_ctx(app):
    """aiohttp cleanup context: run the reaper for the app's lifetime, stop it cleanly."""
    task = asyncio.create_task(_sweeper(app), name="agentctl-sweeper")
    yield
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


def register(app: "web.Application", gate) -> None:
    """Mount the run-ledger routes. *gate* is the ``requires_module('agentctl')`` wrapper."""
    app.router.add_post(f"{BASE}/events", gate(handle_event))
    app.router.add_get(f"{BASE}/runs", gate(handle_runs_list))
    app.router.add_get(f"{BASE}/runs/summary", gate(handle_runs_summary))  # before {run_id}
    app.router.add_get(f"{BASE}/runs/{{run_id}}", gate(handle_run_detail))
    app.router.add_post(f"{BASE}/runs/{{run_id}}/ack", gate(handle_run_ack))
    try:
        app.cleanup_ctx.append(_sweeper_ctx)
    except RuntimeError:  # app already frozen: the routes work, the reaper does not
        logger.warning("navig-agentctl: app is frozen; run reaper not started")
