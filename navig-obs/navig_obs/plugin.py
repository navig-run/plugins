"""navig-obs plugin entry point.

Registers a FREE, toggleable ``obs`` ModuleDef so the Store/deck/os surfaces
show it; the ``obs`` CLI verb itself mounts through the ``navig.commands``
entry point (pyproject). Gateway seams: the show-events service
(``navig_obs.show``) starts with the gateway when ``obs.show.enabled`` is on, and
``GET /api/deck/obs/show`` reports it; the voice remote claims the signal source
``obs.show.voice_source`` (default ``alexa``) as an ingest ACTION — a signed
intent from a voice assistant drives a fixed menu of stage moves
(``navig_obs.show.remote``). It does nothing until that source exists
(``navig signals add alexa``), so the door is closed by default.
"""

from __future__ import annotations

import json
import logging
from typing import Any

_log = logging.getLogger(__name__)
_REGISTERED = False
_BRIDGE: Any = None


def _obs_module_def():
    from navig.modules.registry import ModuleDef, ModuleKind

    return ModuleDef(
        id="obs",
        label="OBS Studio",
        description="Control OBS Studio: scenes, sources, stream, recording (obs-websocket).",
        kind=ModuleKind.APP,
        category="media",
        icon="video",
        capability=None,  # free
        surfaces=["cli:obs"],
        default_enabled=True,
        source="plugin",
    )


_LOOPBACK = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})
# Headers a reverse proxy or tunnel adds: a request carrying one came from
# outside this machine even though the socket says loopback.
_FORWARDED = ("x-forwarded-for", "x-real-ip", "forwarded", "cf-connecting-ip", "cf-ray")
# OBS serves a local-file browser source from this origin (measured 2026-10-16).
_PAGE_ORIGINS = frozenset({"http://absolute"})


def _moment_request_allowed(remote: Any, origin: Any, headers: dict) -> bool:
    if str(remote or "") not in _LOOPBACK:
        return False
    lower = {str(k).lower() for k in headers}
    if any(h in lower for h in _FORWARDED):
        return False
    return str(origin or "") in _PAGE_ORIGINS


def _register_gateway(event: Any) -> None:
    """``gateway:register_routes`` — mount the status route, start the show bridge."""
    app = (getattr(event, "context", None) or {}).get("app")
    if app is None:
        return

    async def handle_status(_request: Any) -> Any:
        from aiohttp import web

        if _BRIDGE is None:
            from navig_obs.show.settings import load_settings

            return web.json_response({"enabled": load_settings().enabled, "running": False})
        return web.json_response(_BRIDGE.status())

    async def _on_startup(_app: Any) -> None:
        global _BRIDGE
        from navig_obs.show.bridge import ShowBridge
        from navig_obs.show.settings import load_settings

        settings = load_settings()
        if not settings.enabled:
            return
        _BRIDGE = ShowBridge(settings)
        await _BRIDGE.start()

    async def _on_cleanup(_app: Any) -> None:
        global _BRIDGE
        if _BRIDGE is not None:
            await _BRIDGE.stop()
            _BRIDGE = None

    async def handle_moment(request: Any) -> Any:
        """``POST /obs/pigeon/moment`` — the pigeon page's sendBeacon.

        Outside /api/deck on purpose: the page is an OBS browser source with no
        deck credential. What guards it instead: loopback only, never a
        tunnelled request, the OBS page origin, a 4 KB body, and the moment
        token the bridge checks (plus its rate limit).
        """
        from aiohttp import web

        if not _moment_request_allowed(
            request.remote, request.headers.get("Origin"), dict(request.headers)
        ):
            return web.Response(status=403)
        if _BRIDGE is None:
            return web.Response(status=503)
        if (request.content_length or 0) > 4096:
            return web.Response(status=413)
        try:
            payload = json.loads((await request.read())[:4096].decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return web.Response(status=400)
        # Answer now, record after: the page's beacon never reads the reply.
        _BRIDGE.moment_soon(payload)
        return web.Response(status=204)

    app.router.add_get("/api/deck/obs/show", handle_status)
    app.router.add_post("/obs/pigeon/moment", handle_moment)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    _register_voice_remote()


def _register_voice_remote() -> None:
    """Claim the voice assistant's signal source for the stage's fixed menu."""
    try:
        from navig.gateway import ingest_actions
    except Exception:  # noqa: BLE001 — an older core without ingest actions
        return
    import asyncio
    import json
    import os

    from navig_obs.show import remote
    from navig_obs.show.bridge import _default_client
    from navig_obs.show.settings import _cfg, _cfg_raw

    source = (os.environ.get("NAVIG_OBS_VOICE_SOURCE") or _cfg("obs.show.voice_source")
              or remote.DEFAULT_SOURCE).strip()
    pages = _cfg_raw("obs.show.voice_pages")
    if isinstance(pages, str):
        try:
            pages = json.loads(pages)
        except ValueError:
            pages = None
    rem = remote.Remote(
        stage=remote.ObsStage(_default_client),
        pages={**remote.DEFAULT_PAGES, **(pages if isinstance(pages, dict) else {})},
    )

    async def action(payload: dict) -> dict:
        try:
            return await asyncio.to_thread(rem.handle, payload)
        except Exception as exc:  # noqa: BLE001 — OBS closed: say so, don't retry
            _log.info("navig-obs: voice remote: %s", exc)
            lang = remote.lang_of_locale(str(payload.get("locale") or ""))
            return {"speech": remote.reply("obs_down", lang), "done": None}

    ingest_actions.register(source, action)


def register() -> None:
    """Idempotently register the ``obs`` module. Called by core's
    ``load_entry_point_plugins()``."""
    global _REGISTERED
    if _REGISTERED:
        return
    try:
        from navig.modules.registry import register_module

        register_module(_obs_module_def())
    except Exception as exc:  # noqa: BLE001 — never block boot
        _log.warning("navig-obs: module registry unavailable (%s)", exc)
    try:
        from navig.core.hooks import register_hook

        register_hook("gateway:register_routes", _register_gateway)
    except Exception as exc:  # noqa: BLE001 — standalone: no gateway
        _log.debug("navig-obs: no gateway hooks (%s)", exc)
    _REGISTERED = True
