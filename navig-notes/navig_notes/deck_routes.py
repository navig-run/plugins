"""``/api/deck/notes/*`` — the notes routes, for the Dock's sync and NAVIG's own surfaces.

Notes are private, so every route answers ONLY this computer (``is_local_request``, as the
chat library does) on top of deck auth and the ``notes`` module gate. Nothing here is reachable
through Lighthouse or the Mini App.

No GET changes anything. All SQLite work runs in a worker thread.

* ``POST /api/deck/notes/sync`` — the Dock's round trip: ``{since, share, notes, deleted}`` →
  ``{cursor, results, changes}`` (see :meth:`NoteStore.sync`). At most ``MAX_SYNC_NOTES`` per
  call; the Dock sends larger sets in pages.
* ``GET /api/deck/notes`` (``site``, ``scope``, ``tag``, ``limit``) · ``GET …/search?q=`` ·
  ``GET …/status`` · ``GET …/{id}``
* ``POST /api/deck/notes`` — a note made by another surface (``origin`` = agent / cli / space).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path
from typing import Any

try:
    from aiohttp import web
except ImportError:  # pragma: no cover — the gateway always has aiohttp
    web = None

from navig_notes.store import ORIGINS, SCOPES, SHARE_LEVELS, NoteError

logger = logging.getLogger(__name__)

MAX_SYNC_NOTES = 500
MAX_SYNC_DELETES = 2000

_store_lock = threading.Lock()
_STORE: dict[str, Any] = {}


def get_store():
    """The one NoteStore for this process (reopened if ``NAVIG_NOTES_DB`` points elsewhere)."""
    from navig_notes.store import NoteStore, default_db_path

    path = str(default_db_path())
    with _store_lock:
        store = _STORE.get(path)
        if store is None:
            store = _STORE[path] = NoteStore(Path(path))
        return store


def _ok(data: object, status: int = 200):
    return web.json_response({"ok": True, "data": data}, status=status)


def _err(msg: str, status: int = 400, **extra):
    return web.json_response({"ok": False, "error": msg, **extra}, status=status)


def _is_local(request) -> bool:
    try:
        from navig.gateway.deck.auth import is_local_request
    except ImportError:  # an older core without the public helper: refuse, never guess
        return False
    return is_local_request(request)


def _local_only(handler):
    async def wrapped(request):
        if not _is_local(request):
            return _err("notes are only reachable from this computer", 403, code="local_only")
        return await handler(request)

    wrapped.__name__ = handler.__name__
    return wrapped


async def _body(request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001 — a missing or malformed body is an empty one
        return {}
    return data if isinstance(data, dict) else {}


def _int(v: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


async def _run(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


# ── handlers ─────────────────────────────────────────────────────────────────


async def handle_sync(request):
    body = await _body(request)
    notes = body.get("notes") or []
    deleted = body.get("deleted") or []
    if not isinstance(notes, list) or not isinstance(deleted, list):
        return _err("notes and deleted must be lists", code="bad_request")
    if len(notes) > MAX_SYNC_NOTES or len(deleted) > MAX_SYNC_DELETES:
        return _err(f"at most {MAX_SYNC_NOTES} notes and {MAX_SYNC_DELETES} deletes per sync — send pages", 413,
                    code="too_many")
    share = body.get("share") if body.get("share") in SHARE_LEVELS else "local"
    since = _int(body.get("since"), 0, 0, 2**62)
    store = get_store()

    def go():
        out = store.sync(notes, deleted, since=since, share=share, origin="dock")
        store.set_meta("last_sync", {"at": int(time.time() * 1000), "pushed": len(notes), "deleted": len(deleted),
                                     "pulled": len(out["changes"]), "share": share})
        return out

    return _ok(await _run(go))


async def handle_list(request):
    q = request.query
    store = get_store()
    scope = q.get("scope") if q.get("scope") in SCOPES else None
    data = await _run(store.list, site=q.get("site") or None, scope=scope, tag=q.get("tag") or None,
                      limit=_int(q.get("limit"), 200, 1, 2000), offset=_int(q.get("offset"), 0, 0, 10**9))
    return _ok({"notes": data})


async def handle_search(request):
    q = (request.query.get("q") or "").strip()
    if not q:
        return _ok({"notes": []})
    store = get_store()
    return _ok({"notes": await _run(store.search, q, limit=_int(request.query.get("limit"), 50, 1, 500))})


async def handle_status(request):  # noqa: ARG001 — aiohttp handler signature
    store = get_store()
    return _ok(await _run(store.stats))


async def handle_get(request):
    store = get_store()
    note = await _run(store.get, request.match_info["id"])
    if note is None:
        return _err("no such note", 404, code="not_found")
    return _ok(note)


async def handle_create(request):
    body = await _body(request)
    text = str(body.get("body") or "")
    if not text.strip() and not str(body.get("title") or "").strip():
        return _err("a note needs some text", code="bad_request")
    origin = body.get("origin") if body.get("origin") in ORIGINS else "agent"
    share = body.get("share") if body.get("share") in SHARE_LEVELS else "local"
    store = get_store()
    try:
        note = await _run(store.create, body=text, title=str(body.get("title") or ""), url=str(body.get("url") or ""),
                          scope=str(body.get("scope") or "global"), tags=body.get("tags"), origin=origin, share=share)
    except NoteError as exc:
        return _err(str(exc), code="bad_request")
    return _ok(note, 201)


def register(app) -> None:
    """Mount the routes: module gate (403 when off) + this-computer-only. Static paths first."""
    from navig.modules.gate import requires_module

    g = requires_module("notes")

    def route(handler):
        return g(_local_only(handler))

    r = app.router
    r.add_get("/api/deck/notes", route(handle_list))
    r.add_post("/api/deck/notes", route(handle_create))
    r.add_post("/api/deck/notes/sync", route(handle_sync))
    r.add_get("/api/deck/notes/search", route(handle_search))
    r.add_get("/api/deck/notes/status", route(handle_status))
    r.add_get("/api/deck/notes/{id}", route(handle_get))


__all__ = ["get_store", "register"]
