"""Email-ops REST handlers — filter rules + briefing schedules + run-now.

Notifications/briefings deliver through the unified notify router, so channel
delivery (deck bell / Telegram / email) follows Settings → Notifications.
"""

from __future__ import annotations

import logging
from typing import Any

try:
    from aiohttp import web
except ImportError:
    web = None

from navig_sdk.files import JsonReadError  # IS navig's class when navig is installed
from navig_email import config as cfg
from navig_email import gmail
from navig_email.service import get_email_service

logger = logging.getLogger(__name__)


def _ok(data: object, status: int = 200) -> "web.Response":
    return web.json_response({"ok": True, "data": data}, status=status)


def _err(msg: str, status: int = 400) -> "web.Response":
    return web.json_response({"ok": False, "error": msg}, status=status)


async def _body(request: "web.Request") -> dict[str, Any]:
    try:
        return await request.json()
    except Exception:
        return {}


def _public(c: dict) -> dict:
    return {
        "monitor_enabled": c.get("monitor_enabled", True),
        "rules": c.get("rules", []),
        "briefings": c.get("briefings", []),
    }


async def handle_email_config_get(request: "web.Request") -> "web.Response":
    try:
        return _ok(_public(cfg.load_config()))
    except Exception as exc:
        logger.exception("email config get failed")
        return _err(str(exc), 500)


async def handle_email_config_save(request: "web.Request") -> "web.Response":
    body = await _body(request)
    try:
        # for_update: a transient lock raises instead of returning empty defaults that
        # would wipe the state/rules we mean to preserve.
        c = cfg.load_config_for_update()  # preserve state
        if "monitor_enabled" in body:
            c["monitor_enabled"] = bool(body["monitor_enabled"])
        if isinstance(body.get("rules"), list):
            c["rules"] = body["rules"]
        if isinstance(body.get("briefings"), list):
            c["briefings"] = body["briefings"]
        cfg.save_config(c)
        return _ok(_public(cfg.load_config()))
    except JsonReadError:
        logger.warning("email config temporarily unreadable — refusing save to avoid a wipe")
        return _err("Email config is temporarily unavailable, please retry.", 503)
    except Exception as exc:
        logger.exception("email config save failed")
        return _err(str(exc), 500)


async def handle_email_brief_run(request: "web.Request") -> "web.Response":
    body = await _body(request)
    try:
        return _ok(await get_email_service().run_brief_now(body.get("id")))
    except Exception as exc:
        logger.exception("email brief run failed")
        return _err(str(exc), 500)


async def handle_email_status(request: "web.Request") -> "web.Response":
    try:
        c = cfg.load_config()
        svc = get_email_service()
        return _ok({
            "connected": gmail.is_connected(),
            "monitor_enabled": c.get("monitor_enabled", True),
            "rules": len(c.get("rules", [])),
            "briefings": len(c.get("briefings", [])),
            "last_check": svc.last_check.isoformat() if svc.last_check else None,
            "last_error": svc.last_error,
        })
    except Exception as exc:
        logger.exception("email status failed")
        return _err(str(exc), 500)


# ── the Mailroom view (navig OS) ────────────────────────────────────────────
#
# One GET builds the whole page (edge + gmail + paper + cron, each degrading on its own),
# two GETs page the two lists, and one POST runs a mailroom verb. The POST takes an ACTION
# NAME, never a command line: the UI may ask for "scan the paper inbox", it may not ask for
# an arbitrary process.

#: action name → argv template (`{space}` is substituted, nothing else is interpolated).
MAILROOM_ACTIONS: dict[str, tuple[str, ...]] = {
    "watch": ("navig", "email", "watch", "--space", "{space}", "--yes", "--json"),
    "stats": ("navig", "email", "stats", "--space", "{space}", "--period", "week", "--json"),
    "edge_stats_send": ("navig", "email", "edge", "stats", "--space", "{space}", "--send", "--json"),
    "scan": (
        "navig", "paperwork", "scan", "inbox", "--space", "{space}",
        "--profile", "personal", "--apply", "--yes", "--json",
    ),
    "echeances": ("navig", "paperwork", "echeances", "--space", "{space}", "--json"),
}
ACTION_TIMEOUT = 600


def _space_arg(request: "web.Request") -> str | None:
    value = (request.query.get("space") or "").strip()
    return value or None


def _limit(request: "web.Request", default: int, cap: int) -> int:
    raw = (request.query.get("limit") or "").strip()
    if not raw:
        return default
    try:
        return max(1, min(cap, int(raw)))
    except ValueError:
        raise ValueError(f"limit must be an integer (1-{cap})") from None


async def handle_mailroom_overview(request: "web.Request") -> "web.Response":
    from navig_email.overview import build_overview

    try:
        return _ok(await build_overview(_space_arg(request)))
    except Exception as exc:  # noqa: BLE001
        logger.exception("mailroom overview failed")
        return _err(str(exc), 500)


async def handle_mailroom_edge_events(request: "web.Request") -> "web.Response":
    import asyncio

    from navig_email import edge as E
    from navig_email.overview import resolve_space

    try:
        limit = _limit(request, 50, 500)
    except ValueError as exc:
        return _err(str(exc), 400)
    try:
        paths = resolve_space(_space_arg(request))
        if paths is None:
            return _err("no space — pass ?space=<name> or set mailroom.paper_space", 400)
        events = await asyncio.to_thread(E.events, paths, limit=limit)
        return _ok({"events": events})
    except E.EdgeNotConfigured as exc:
        return _err(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        logger.exception("mailroom edge events failed")
        return _err(str(exc), 500)


async def handle_mailroom_paper_ledger(request: "web.Request") -> "web.Response":
    import asyncio

    from navig_email.overview import _read_jsonl, resolve_space

    try:
        limit = _limit(request, 50, 500)
    except ValueError as exc:
        return _err(str(exc), 400)
    try:
        paths = resolve_space(_space_arg(request))
        if paths is None:
            return _err("no space — pass ?space=<name> or set mailroom.paper_space", 400)
        path = paths.space_root / "mailroom" / "ledger" / "courrier.jsonl"
        rows = await asyncio.to_thread(_read_jsonl, path, limit=limit)
        return _ok({"filed": rows, "ledger": str(path)})
    except Exception as exc:  # noqa: BLE001
        logger.exception("mailroom paper ledger failed")
        return _err(str(exc), 500)


async def handle_mailroom_action(request: "web.Request") -> "web.Response":
    """Run one named mailroom verb. The name is looked up in MAILROOM_ACTIONS — the caller
    never supplies a command, an argument or a flag."""
    import asyncio

    from navig_email.overview import resolve_space

    body = await _body(request)
    action = str(body.get("action") or "").strip()
    template = MAILROOM_ACTIONS.get(action)
    if template is None:
        return _err(f"unknown action {action!r} (expected: {', '.join(sorted(MAILROOM_ACTIONS))})", 400)
    try:
        paths = resolve_space(str(body.get("space") or "") or None)
    except ValueError as exc:
        return _err(str(exc), 400)
    if paths is None:
        return _err("no space — pass {\"space\": …} or set mailroom.paper_space", 400)

    argv = [part.replace("{space}", str(paths.space_root)) for part in template]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
    except OSError as exc:
        return _err(f"cannot run {argv[0]}: {exc}", 500)
    try:
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout=ACTION_TIMEOUT)
    except (TimeoutError, asyncio.TimeoutError):
        proc.kill()
        return _err(f"{action} timed out after {ACTION_TIMEOUT}s", 504)

    from navig.core.proc_text import decode_console_output

    text = decode_console_output(raw)
    payload: dict[str, Any] = {"action": action, "exit_code": int(proc.returncode or 0)}
    start = text.find("{")
    if start >= 0:
        import json as _json

        try:
            payload["result"] = _json.loads(text[start:])
        except _json.JSONDecodeError:
            payload["output"] = text[-2000:]
    else:
        payload["output"] = text[-2000:]
    if payload["exit_code"] != 0:
        return _err(f"{action} exited {payload['exit_code']}: {text[-300:]}", 502)
    return _ok(payload)


def register(app: "web.Application") -> None:
    """Mount the Email deck routes, each gated on the free "email" module being
    enabled (registry toggle → 403 module_disabled). Called from the navig-email
    plugin's gateway:register_routes hook."""
    from navig.modules.gate import requires_module

    g = requires_module("email")
    app.router.add_get("/api/deck/email/config", g(handle_email_config_get))
    app.router.add_post("/api/deck/email/config", g(handle_email_config_save))
    app.router.add_post("/api/deck/email/brief/run", g(handle_email_brief_run))
    app.router.add_get("/api/deck/email/status", g(handle_email_status))
    app.router.add_get("/api/deck/email/mailroom/overview", g(handle_mailroom_overview))
    app.router.add_get("/api/deck/email/mailroom/edge/events", g(handle_mailroom_edge_events))
    app.router.add_get("/api/deck/email/mailroom/paper/ledger", g(handle_mailroom_paper_ledger))
    app.router.add_post("/api/deck/email/mailroom/action", g(handle_mailroom_action))
