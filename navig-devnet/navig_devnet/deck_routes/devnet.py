"""Deck API for the DevNet Lab tile — the HTTP repeater, surfaced to the OS UI.

Routes live under ``/api/deck/devnet/*`` and are gated on the ``devnet`` module
being enabled (``requires_module`` → ``403 module_disabled`` when off, live, no
restart), so the per-space toggle governs the tile exactly as it governs the CLI
verb and the agent tools. Lookups (DNS/SSL/WHOIS) already have a home in the
NetTools tile; this surface is the repeater — the genuinely new thing.

Handlers never 500 the gateway: a bad request is a structured 4xx, an unexpected
error a structured 500 with the message.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiohttp import web

_log = logging.getLogger(__name__)


def _ok(data, status: int = 200):
    from aiohttp import web

    return web.json_response({"ok": True, "data": data}, status=status)


def _err(msg: str, status: int = 500):
    from aiohttp import web

    return web.json_response({"ok": False, "error": msg}, status=status)


async def handle_http(request: "web.Request") -> "web.Response":
    """POST — send one request through the repeater and record it in history.

    Body: ``{method, url, headers?, body?, allow_private?, follow_redirects?}``.
    """
    from navig_sdk.ssrf import SsrfBlockedError

    from navig_devnet import history
    from navig_devnet.http_repeater import send

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 — malformed JSON is a client error, not a 500
        return _err("invalid JSON body", status=400)
    url = (payload or {}).get("url")
    if not url or not isinstance(url, str):
        return _err("missing 'url'", status=400)
    try:
        result = await send(
            str(payload.get("method", "GET")),
            url,
            headers=payload.get("headers") or None,
            body=payload.get("body"),
            allow_private=bool(payload.get("allow_private", False)),
            follow_redirects=bool(payload.get("follow_redirects", True)),
        )
    except SsrfBlockedError as exc:
        return _err(str(exc), status=400)
    except ValueError as exc:
        return _err(str(exc), status=400)
    except Exception as exc:  # noqa: BLE001 — transport failure, not a gateway fault
        _log.warning("devnet http route: %s", exc)
        return _err(f"{type(exc).__name__}: {exc}", status=502)
    hid = history.record(result)
    if hid is not None:
        result["history_id"] = hid
    return _ok(result)


async def handle_history(request: "web.Request") -> "web.Response":
    """GET ?limit=N — recent repeater requests in the active space."""
    from navig_devnet import history

    try:
        limit = int(request.query.get("limit", "50"))
    except ValueError:
        limit = 50
    return _ok(history.load(limit=max(1, min(limit, 500))))


async def handle_crawl(request: "web.Request") -> "web.Response":
    """POST — a read-only, same-origin, robots-respecting crawl.

    Body: ``{url, max_pages?, max_depth?, same_origin?, respect_robots?,
    allow_private?}``. Returns the crawl map (pages with status/title/links).
    """
    from navig_sdk.ssrf import SsrfBlockedError

    from navig_devnet.crawl import CrawlError, crawl

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 — malformed JSON is a client error
        return _err("invalid JSON body", status=400)
    url = (payload or {}).get("url")
    if not url or not isinstance(url, str):
        return _err("missing 'url'", status=400)
    try:
        result = await crawl(
            url,
            max_pages=int(payload.get("max_pages", 25) or 25),
            max_depth=int(payload.get("max_depth", 2) or 2),
            same_origin=bool(payload.get("same_origin", True)),
            respect_robots=bool(payload.get("respect_robots", True)),
            allow_private=bool(payload.get("allow_private", False)),
        )
    except (SsrfBlockedError, CrawlError, ValueError) as exc:
        return _err(str(exc), status=400)
    except Exception as exc:  # noqa: BLE001 — transport failure, not a gateway fault
        _log.warning("devnet crawl route: %s", exc)
        return _err(f"{type(exc).__name__}: {exc}", status=502)
    return _ok(result)


async def handle_api(request: "web.Request") -> "web.Response":
    """POST — discover and parse an OpenAPI/Swagger spec for a base URL.

    Body: ``{url, allow_private?}``. Returns the spec's title, servers and
    endpoints, or a 404 when no spec is found.
    """
    from navig_sdk.ssrf import SsrfBlockedError

    from navig_devnet.crawl import CrawlError, discover_api

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return _err("invalid JSON body", status=400)
    url = (payload or {}).get("url")
    if not url or not isinstance(url, str):
        return _err("missing 'url'", status=400)
    try:
        result = await discover_api(url, allow_private=bool(payload.get("allow_private", False)))
    except SsrfBlockedError as exc:
        return _err(str(exc), status=400)
    except CrawlError as exc:
        # "no spec found" / "invalid URL" — a 404 reads truer than a 400 here.
        return _err(str(exc), status=404)
    except ValueError as exc:
        return _err(str(exc), status=400)
    except Exception as exc:  # noqa: BLE001
        _log.warning("devnet api route: %s", exc)
        return _err(f"{type(exc).__name__}: {exc}", status=502)
    return _ok(result)


def register(app: "web.Application") -> None:
    """Mount the DevNet deck routes, gated on the ``devnet`` module."""
    from navig.modules.gate import requires_module

    g = requires_module("devnet")
    app.router.add_post("/api/deck/devnet/http", g(handle_http))
    app.router.add_get("/api/deck/devnet/history", g(handle_history))
    app.router.add_post("/api/deck/devnet/crawl", g(handle_crawl))
    app.router.add_post("/api/deck/devnet/api", g(handle_api))
