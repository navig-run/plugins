"""The Cabinet's deck routes — for the desktop app, and ONLY from this computer.

The deck API is also reachable remotely: through the Lighthouse edge, a Cloudflare
tunnel, the Telegram Mini App. None of that should ever see a list of someone's medical
documents or be able to open a passport scan. So every route here first asks
``is_local_request`` (the same test the desktop auth bypass uses: tunnel traffic always
carries ``CF-Ray``/``CF-Connecting-IP``, which a remote caller cannot strip) and answers
403 otherwise — on top of the normal deck auth and the ``cabinet`` module gate.

No file content crosses HTTP. "Open" decrypts into the cabinet's short-lived ``.open/``
folder on this machine and launches the usual viewer; "add" takes paths on this machine.
The routes return metadata only — never the OCR text, only a snippet around a search hit.

A passphrase-locked cabinet is unlocked with ``POST /cabinet/unlock``; the key is held in
this process for :data:`SESSION_IDLE_SECONDS` of inactivity, then dropped.

All crypto, SQLite and OCR work runs in a worker thread: scrypt alone would stall the
gateway's event loop for every other request.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

try:
    from aiohttp import web
except ImportError:  # pragma: no cover — the gateway always has aiohttp
    web = None

logger = logging.getLogger(__name__)

SESSION_IDLE_SECONDS = 15 * 60
MAX_ADD_PATHS = 200

_lock = threading.Lock()
_session: dict[str, Any] = {"master": None, "root": None, "last": 0.0}


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
            return _err("the cabinet is only reachable from this computer", 403, code="local_only")
        return await handler(request)

    wrapped.__name__ = handler.__name__
    return wrapped


async def _body(request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001 — a missing or malformed body is an empty one
        return {}
    return data if isinstance(data, dict) else {}


# ── opening the cabinet inside the gateway ──────────────────────────────────


class Locked(Exception):
    pass


def _root() -> Path:
    from .store import default_root

    return default_root()


def _session_master(root: Path) -> bytes | None:
    with _lock:
        if _session["master"] is None or _session["root"] != str(root):
            return None
        if time.monotonic() - _session["last"] > SESSION_IDLE_SECONDS:
            _session.update(master=None, root=None)
            return None
        _session["last"] = time.monotonic()
        return _session["master"]


def _open():
    """An open Cabinet, or raise Locked / FileNotFoundError. Runs in a worker thread."""
    from . import keys
    from .store import Cabinet

    root = _root()
    if not Cabinet.exists(root):
        raise FileNotFoundError(str(root))
    kf = keys.KeyFile.load(root)
    if kf.mode == "passphrase":
        master = _session_master(root)
        if master is None:
            raise Locked()
        return Cabinet(root, master)
    return Cabinet(root, kf.unwrap(None))


def _item_json(it, snippet: str | None = None) -> dict:
    d = it.public()
    d["days_to_expiry"] = it.days_to_expiry()
    if snippet is not None:
        d["snippet"] = snippet
    return d


def _snippet(text: str, term: str, width: int = 50) -> str:
    i = text.casefold().find(term)
    if i < 0:
        return ""
    start, end = max(0, i - width), min(len(text), i + len(term) + width)
    s = " ".join(text[start:end].split())
    return ("…" if start else "") + s + ("…" if end < len(text) else "")


async def _with_cabinet(fn):
    """Run ``fn(cabinet)`` off the event loop, mapping the lock states to responses."""
    def work():
        cab = _open()
        try:
            return fn(cab)
        finally:
            cab.close()

    try:
        return _ok(await asyncio.to_thread(work))
    except FileNotFoundError:
        return _ok({"exists": False})
    except Locked:
        return _err("the cabinet is locked", 423, code="locked")
    except Exception as exc:  # noqa: BLE001 — surface it; never a silent empty list
        logger.warning("cabinet deck route failed: %s", exc)
        return _err(str(exc), 500)


# ── handlers ────────────────────────────────────────────────────────────────


async def handle_status(request):
    def status():
        from . import keys, reminders
        from .store import Cabinet

        root = _root()
        if not Cabinet.exists(root):
            return {"exists": False, "path": str(root)}
        mode = keys.KeyFile.load(root).mode
        base = {"exists": True, "path": str(root), "lock": mode, "reminders": reminders.enabled()}
        try:
            backup = json.loads((root / "backup.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            backup = {}
        base["last_backup_at"] = backup.get("at")
        try:
            cab = _open()
        except Locked:
            return {**base, "unlocked": False}
        try:
            items = cab.items()
        finally:
            cab.close()
        by_cat: dict[str, int] = {}
        for it in items:
            by_cat[it.category] = by_cat.get(it.category, 0) + 1
        expiring = sorted((i for i in items if (d := i.days_to_expiry()) is not None and d <= 90),
                          key=lambda i: i.expires or "")
        return {**base, "unlocked": True, "items": len(items), "bytes": sum(i.size for i in items),
                "by_category": by_cat, "expiring": [_item_json(i) for i in expiring]}

    try:
        return _ok(await asyncio.to_thread(status))
    except Exception as exc:  # noqa: BLE001
        logger.warning("cabinet status failed: %s", exc)
        return _err(str(exc), 500)


async def handle_items(request):
    q = (request.query.get("q") or "").strip()
    category = (request.query.get("category") or "").strip().lower() or None
    terms = [t.casefold() for t in q.split() if t]

    def items(cab):
        out = []
        for it in cab.items():
            if category and it.category != category:
                continue
            if terms:
                hay = " ".join([it.title, it.original_name, it.category, " ".join(it.tags),
                                it.issuer or "", it.notes or "", it.text]).casefold()
                if not all(t in hay for t in terms):
                    continue
                out.append(_item_json(it, _snippet(it.text, terms[0])))
            else:
                out.append(_item_json(it))
        return {"items": out}

    return await _with_cabinet(items)


async def handle_open(request):
    body = await _body(request)
    ref = str(body.get("id") or "")
    if not ref:
        return _err("id is required")

    def open_it(cab):
        from .commands.cabinet import _launch

        it = cab.resolve(ref)
        path = cab.open_copy(it)
        _launch(path)
        return {"id": it.id, "opened": True}

    return await _with_cabinet(open_it)


async def handle_add(request):
    body = await _body(request)
    raw = body.get("paths") or []
    if not isinstance(raw, list) or not raw:
        return _err("paths must be a non-empty list of files on this computer")
    if len(raw) > MAX_ADD_PATHS:
        return _err(f"at most {MAX_ADD_PATHS} files at a time")
    paths = [Path(str(p)) for p in raw]
    if any(not p.is_absolute() for p in paths):
        return _err("paths must be absolute")
    category = body.get("category") or None
    expires = body.get("expires") or None

    def add(cab):
        from .categories import normalise_category
        from .ingest import add_path, expand
        from .store import CabinetError, Duplicate

        cat = normalise_category(category) if category else None
        added, duplicates, failed = [], [], []
        for f in expand(paths):
            try:
                added.append(_item_json(add_path(cab, f, category=cat, expires=expires).item))
            except Duplicate as exc:
                duplicates.append({"path": str(f), "existing": exc.existing.id})
            except (CabinetError, OSError, ValueError) as exc:
                failed.append({"path": str(f), "error": str(exc)})
        return {"added": added, "duplicates": duplicates, "failed": failed}

    def create_then_add():
        from .store import Cabinet

        root = _root()
        if not Cabinet.exists(root):
            Cabinet.create(root).close()

    await asyncio.to_thread(create_then_add)
    return await _with_cabinet(add)


async def handle_unlock(request):
    body = await _body(request)
    passphrase = body.get("passphrase")
    if not isinstance(passphrase, str) or not passphrase:
        return _err("passphrase is required")

    def unlock():
        from . import keys

        root = _root()
        master = keys.KeyFile.load(root).unwrap(passphrase)
        with _lock:
            _session.update(master=master, root=str(root), last=time.monotonic())

    try:
        await asyncio.to_thread(unlock)
    except Exception as exc:  # noqa: BLE001 — WrongKey and friends
        return _err(str(exc) or "wrong passphrase", 403, code="wrong_passphrase")
    return _ok({"unlocked": True, "idle_minutes": SESSION_IDLE_SECONDS // 60})


async def handle_lock(request):
    with _lock:
        _session.update(master=None, root=None, last=0.0)
    return _ok({"unlocked": False})


def register(app) -> None:
    """Mount the routes: module gate (403 when off) + this-computer-only."""
    from navig.modules.gate import requires_module

    g = requires_module("cabinet")

    def route(handler):
        return g(_local_only(handler))

    app.router.add_get("/api/deck/cabinet/status", route(handle_status))
    app.router.add_get("/api/deck/cabinet/items", route(handle_items))
    app.router.add_post("/api/deck/cabinet/open", route(handle_open))
    app.router.add_post("/api/deck/cabinet/add", route(handle_add))
    app.router.add_post("/api/deck/cabinet/unlock", route(handle_unlock))
    app.router.add_post("/api/deck/cabinet/lock", route(handle_lock))
