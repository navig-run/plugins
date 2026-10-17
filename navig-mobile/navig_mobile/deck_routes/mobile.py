"""navig-mobile REST handlers — read-mostly device dashboard data + screenshot.

Served at ``/api/deck/mobile/*`` (the desktop OS proxies the same ``/api/deck/*``
namespace). Every route is gated on the free ``mobile`` module being enabled
(registry toggle → 403 ``module_disabled``). Investigative / destructive verbs
(forensics, spyware scan, root/flash, backup) are intentionally **not** exposed
here — they stay CLI-only, behind the consent gate and typed confirmation.
"""

from __future__ import annotations

import base64
import logging

try:
    from aiohttp import web
except ImportError:  # pragma: no cover — aiohttp ships with navig-core
    web = None

logger = logging.getLogger(__name__)

# Inline a screenshot as a data URI only when it's small enough for JSON.
_MAX_INLINE = 3 * 1024 * 1024


def _ok(data: object, status: int = 200) -> "web.Response":
    return web.json_response({"ok": True, "data": data}, status=status)


def _err(msg: str, status: int = 400) -> "web.Response":
    return web.json_response({"ok": False, "error": msg}, status=status)


def _manager():
    from navig_mobile.engine.base import DeviceManager

    return DeviceManager()


def _resolve_or_error(udid: str):
    """Return (device, None) or (None, error_response)."""
    from navig_mobile.engine.base import DeviceError

    try:
        return _manager().resolve(udid=udid), None
    except DeviceError as exc:
        return None, _err(str(exc), 404)


# ── read endpoints ───────────────────────────────────────────────────────────

async def handle_devices(request: "web.Request") -> "web.Response":
    try:
        infos = _manager().list_devices()
        return _ok([i.to_dict() for i in infos])
    except Exception:  # pragma: no cover — defensive
        logger.exception("mobile devices failed")
        return _err("internal error", 500)


async def handle_doctor(request: "web.Request") -> "web.Response":
    try:
        from navig_mobile import tools

        tc = tools.detect()
        return _ok({
            "android_ready": tc.android_ready,
            "ios_ready": tc.ios_ready,
            "uiauto_ready": tc.uiauto_ready,
            "tools": [t.__dict__ for t in tc.tools],
        })
    except Exception:  # pragma: no cover
        logger.exception("mobile doctor failed")
        return _err("internal error", 500)


async def handle_device_info(request: "web.Request") -> "web.Response":
    dev, err = _resolve_or_error(request.match_info["udid"])
    if err:
        return err
    try:
        return _ok(dev.info().to_dict())
    except Exception:
        logger.exception("mobile device info failed")
        return _err("internal error", 500)


async def handle_device_apps(request: "web.Request") -> "web.Response":
    dev, err = _resolve_or_error(request.match_info["udid"])
    if err:
        return err
    system = request.query.get("system") in ("1", "true", "yes")
    try:
        return _ok([a.to_dict() for a in dev.apps(system=system)])
    except Exception:
        logger.exception("mobile device apps failed")
        return _err("internal error", 500)


async def handle_backups(request: "web.Request") -> "web.Response":
    try:
        from navig_mobile.store import get_store

        udid = request.query.get("udid")
        return _ok(get_store().list_backups(udid))
    except Exception:  # pragma: no cover
        logger.exception("mobile backups failed")
        return _err("internal error", 500)


async def handle_cases(request: "web.Request") -> "web.Response":
    try:
        from navig_mobile.store import get_store

        return _ok(get_store().list_cases())
    except Exception:  # pragma: no cover
        logger.exception("mobile cases failed")
        return _err("internal error", 500)


# ── action endpoints (non-destructive) ───────────────────────────────────────

async def handle_screenshot(request: "web.Request") -> "web.Response":
    from pathlib import Path

    from navig_mobile import config
    from navig_mobile.engine.base import DeviceError

    dev, err = _resolve_or_error(request.match_info["udid"])
    if err:
        return err
    dest_dir = config.mobile_dir() / "screenshots"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{dev.udid.replace(':', '_')}.png"
    try:
        saved = dev.screenshot(str(dest))
    except DeviceError as exc:
        return _err(str(exc), 502)
    except Exception:
        logger.exception("mobile screenshot failed")
        return _err("internal error", 500)
    data = {"path": saved}
    try:
        raw = Path(saved).read_bytes()
        data["bytes"] = len(raw)
        if len(raw) <= _MAX_INLINE:
            data["data_uri"] = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
    except Exception:
        pass  # path is still returned; inline preview is best-effort
    return _ok(data)


def register(app: "web.Application") -> None:
    """Mount the mobile deck routes, each gated on the free ``mobile`` module being
    enabled. Called from the plugin's ``gateway:register_routes`` hook."""
    from navig.modules.gate import requires_module

    g = requires_module("mobile")
    app.router.add_get("/api/deck/mobile/devices", g(handle_devices))
    app.router.add_get("/api/deck/mobile/doctor", g(handle_doctor))
    app.router.add_get("/api/deck/mobile/backups", g(handle_backups))
    app.router.add_get("/api/deck/mobile/cases", g(handle_cases))
    app.router.add_get("/api/deck/mobile/device/{udid}", g(handle_device_info))
    app.router.add_get("/api/deck/mobile/device/{udid}/apps", g(handle_device_apps))
    app.router.add_post("/api/deck/mobile/device/{udid}/screenshot", g(handle_screenshot))
    logger.debug("navig-mobile: deck routes mounted")
