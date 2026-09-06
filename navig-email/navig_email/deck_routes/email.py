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

from navig.core.json_io import JsonReadError
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
