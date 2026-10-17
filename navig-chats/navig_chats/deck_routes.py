"""``/api/deck/chats/*`` — the chat library's routes, for the Dock and NAVIG's own surfaces.

Chat histories are private, so every route answers ONLY this computer (``is_local_request``,
the same test navig-cabinet uses: tunnelled traffic always carries Cloudflare headers a remote
caller cannot strip) on top of deck auth and the ``chats`` module gate. Nothing here is
reachable through Lighthouse or the Mini App.

No GET changes anything. Downloads use a single-use token minted by ``POST /export``, so a web
page that points an ``<img>`` at the daemon cannot trigger an export, and long id lists never
ride in a URL.

All SQLite work runs in a worker thread so a big search or import never stalls the gateway.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

try:
    from aiohttp import web
except ImportError:  # pragma: no cover — the gateway always has aiohttp
    web = None

from navig_chats.models import ROLES, ConversationIn, MessageIn, conversation_id, url_native_id

logger = logging.getLogger(__name__)

MAX_CAPTURE_MESSAGES = 5000
MAX_CAPTURE_CHARS = 20_000_000
MAX_UPLOAD_BYTES = 4 * 1024**3
EXPORT_TOKEN_TTL = 300

_store_lock = threading.Lock()
_STORE: dict[str, Any] = {}
_tokens: dict[str, dict[str, Any]] = {}
_tokens_lock = threading.Lock()


def get_store():
    """The one ChatStore for this process (reopened if ``NAVIG_CHATS_DB`` points elsewhere)."""
    from navig_chats.store import ChatStore, default_db_path

    path = str(default_db_path())
    with _store_lock:
        store = _STORE.get(path)
        if store is None:
            store = _STORE[path] = ChatStore(Path(path))
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
            return _err("the chat library is only reachable from this computer", 403, code="local_only")
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


def _not_found(exc: Exception):
    return _err(f"not found: {exc}", 404, code="not_found")


# ── reading ──────────────────────────────────────────────────────────────────


async def handle_list(request):
    q = request.query
    rows, total = await _run(
        get_store().list,
        folder=q.get("folder") or None,
        tag=q.get("tag") or None,
        provider=q.get("provider") or None,
        collection=q.get("collection") or "all",
        project=q.get("project") or None,
        sort=q.get("sort") or "newest",
        limit=_int(q.get("limit"), 100, 1, 1000),
        offset=_int(q.get("offset"), 0, 0, 10**9),
    )
    return _ok({"items": rows, "total": total})


async def handle_overview(request):
    store = get_store()

    def build():
        return {
            "folders": store.folders(),
            "collections": store.collection_counts(),
            "tags": store.tag_counts(),
            "projects": store.project_counts(),
            "stats": store.stats(),
        }

    return _ok(await _run(build))


async def handle_search(request):
    from navig_chats.query import EmptyQuery

    q = request.query
    text = (q.get("q") or "").strip()
    if not text:
        return _ok({"query": "", "terms": [], "results": []})
    try:
        data = await _run(
            get_store().search,
            text,
            folder=q.get("folder") or None,
            tag=q.get("tag") or None,
            provider=q.get("provider") or None,
            project=q.get("project") or None,
            role=q.get("role") if q.get("role") in ROLES else None,
            code_only=q.get("code") in ("1", "true"),
            completeness=q.get("completeness") or None,
            since=q.get("since") or None,
            until=q.get("until") or None,
            conversation=q.get("conversation") or None,
            include_archived=q.get("archived") != "0",
            limit=_int(q.get("limit"), 30, 1, 200),
        )
    except EmptyQuery:
        return _ok({"query": text, "terms": [], "results": [], "note": "nothing to search for — add a word that is not excluded"})
    return _ok(data)


async def handle_get(request):
    store = get_store()
    cid = request.match_info["id"]
    conv = await _run(store.get, cid)
    if conv is None:
        return _err("no such chat in the library", 404, code="not_found")
    return _ok(conv)


async def handle_messages(request):
    store = get_store()
    cid = request.match_info["id"]
    branch = "all" if request.query.get("branch") == "all" else "active"
    conv = await _run(store.get, cid)
    if conv is None:
        return _err("no such chat in the library", 404, code="not_found")
    return _ok({"conversation": conv, "messages": await _run(store.messages, cid, branch=branch)})


# ── capture from the page ────────────────────────────────────────────────────


def _capture_payload(body: dict[str, Any]) -> ConversationIn:
    provider = str(body.get("provider") or "chatgpt")
    url = str(body.get("url") or "")[:2000]
    native = str(body.get("native_id") or "").strip()[:200] or (url_native_id(url) if url else "")
    if not native:
        raise ValueError("a capture needs native_id or url")
    raw_msgs = body.get("messages") or []
    if not isinstance(raw_msgs, list) or len(raw_msgs) > MAX_CAPTURE_MESSAGES:
        raise ValueError(f"messages must be a list of at most {MAX_CAPTURE_MESSAGES}")
    total = 0
    messages: list[MessageIn] = []
    for i, m in enumerate(raw_msgs):
        if not isinstance(m, dict):
            raise ValueError(f"message {i} is not an object")
        role = m.get("role")
        if role not in ROLES:
            raise ValueError(f"message {i} has an unknown role")
        text = str(m.get("text") or "")
        total += len(text)
        if total > MAX_CAPTURE_CHARS:
            raise ValueError("capture too large")
        mid = str(m.get("id") or "").strip()[:200] or f"dom-{i}"
        messages.append(MessageIn(id=mid, role=role, text=text, has_code=bool(m.get("has_code")),
                                  created_at=m.get("created_at") if isinstance(m.get("created_at"), str) else None))
    if messages:
        completeness = body.get("completeness") if body.get("completeness") in ("partial", "rendered") else "partial"
    else:
        completeness = "url_only"
    return ConversationIn(
        provider=provider,
        native_id=native,
        title=str(body.get("title") or "")[:500],
        url=url,
        messages=messages,
        completeness=completeness,
        provenance="live" if body.get("source") != "manual" else "manual",
        remote_project_id=(str(body["project_id"])[:200] if body.get("project_id") else None),
        remote_project_name=(str(body["project_name"])[:200] if body.get("project_name") else None),
        gizmo_id=(str(body["gizmo_id"])[:200] if body.get("gizmo_id") else None),
    )


async def handle_capture(request):
    from navig_chats.store import NotFound

    body = await _body(request)
    try:
        conv = _capture_payload(body)
    except ValueError as exc:
        return _err(str(exc), 400, code="bad_capture")
    store = get_store()

    def save():
        res = store.upsert(conv)
        if body.get("folder_id"):
            store.bulk([conv.id], "add_to", str(body["folder_id"]))
        return {**res, "conversation": store.get(conv.id)}

    try:
        return _ok(await _run(save))
    except NotFound as exc:
        return _not_found(exc)


async def handle_visit(request):
    body = await _body(request)
    provider = str(body.get("provider") or "chatgpt")
    native = str(body.get("native_id") or "").strip()
    if not native:
        return _err("native_id is required", 400)
    conv = await _run(get_store().record_visit, conversation_id(provider, native))
    return _ok({"saved": conv is not None, "conversation": conv})


# ── organisation ─────────────────────────────────────────────────────────────


async def handle_patch(request):
    from navig_chats.store import NotFound

    body = await _body(request)
    allowed = {k: body[k] for k in ("alias", "pinned", "favorite", "archived", "sort_key", "tags", "folders", "brain")
               if k in body}
    try:
        return _ok(await _run(get_store().update, request.match_info["id"], **allowed))
    except NotFound as exc:
        return _not_found(exc)
    except (TypeError, ValueError) as exc:
        return _err(str(exc), 400)


async def handle_delete(request):
    gone = await _run(get_store().delete, request.match_info["id"])
    return _ok({"deleted": gone}) if gone else _err("no such chat in the library", 404, code="not_found")


async def handle_bulk(request):
    from navig_chats.store import NotFound

    body = await _body(request)
    ids = body.get("ids") or []
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        return _err("ids must be a list of chat ids", 400)
    try:
        changed = await _run(get_store().bulk, ids[:10000], str(body.get("action") or ""), body.get("value"))
    except NotFound as exc:
        return _not_found(exc)
    except ValueError as exc:
        return _err(str(exc), 400)
    return _ok({"changed": changed})


async def handle_folders(request):
    return _ok(await _run(get_store().folders))


async def handle_folder_create(request):
    from navig_chats.store import NotFound

    body = await _body(request)
    try:
        return _ok(await _run(get_store().create_folder, str(body.get("name") or ""),
                              parent_id=body.get("parent_id") or None, color=body.get("color"), icon=body.get("icon")), 201)
    except NotFound as exc:
        return _not_found(exc)
    except ValueError as exc:
        return _err(str(exc), 400)


async def handle_folder_patch(request):
    from navig_chats.store import NotFound

    body = await _body(request)
    allowed = {k: body[k] for k in ("name", "color", "icon", "pinned", "sort_key", "parent_id", "brain", "brain_project")
               if k in body}
    try:
        return _ok(await _run(get_store().update_folder, request.match_info["fid"], **allowed))
    except NotFound as exc:
        return _not_found(exc)
    except ValueError as exc:
        return _err(str(exc), 400)


async def handle_folder_delete(request):
    gone = await _run(get_store().delete_folder, request.match_info["fid"])
    return _ok({"deleted": gone}) if gone else _err("no such folder", 404, code="not_found")


async def handle_bookmark_add(request):
    from navig_chats.store import NotFound

    body = await _body(request)
    try:
        return _ok(await _run(get_store().add_bookmark, str(body.get("conversation_id") or ""),
                              message_id=body.get("message_id"), kind=str(body.get("kind") or "bookmark"),
                              note=body.get("note"), quote=body.get("quote")), 201)
    except NotFound as exc:
        return _not_found(exc)
    except ValueError as exc:
        return _err(str(exc), 400)


async def handle_bookmark_delete(request):
    gone = await _run(get_store().remove_bookmark, request.match_info["bid"])
    return _ok({"deleted": gone}) if gone else _err("no such bookmark", 404, code="not_found")


# ── import ───────────────────────────────────────────────────────────────────


def _imports_dir() -> Path:
    from navig_sdk.host import data_dir

    d = data_dir() / "chats-imports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _start_import_thread(path: Path, *, delete_after: bool) -> str:
    from navig_chats.chatgpt_export import import_export

    store = get_store()
    jid = store.start_import(path.name)

    def work():
        try:
            import_export(store, path, job_id=jid)
        except Exception as exc:  # noqa: BLE001 — the job row carries the failure to the surface
            logger.warning("chat import %s failed: %s", jid, exc)
            store.update_import(jid, status="failed", errors=[{"error": str(exc)}])
        finally:
            if delete_after:
                path.unlink(missing_ok=True)

    threading.Thread(target=work, name=f"chats-import-{jid}", daemon=True).start()
    return jid


async def handle_import(request):
    """Start an import. Body: multipart ``file`` (the ZIP or JSON), or JSON ``{"path"}`` on this machine."""
    ctype = request.headers.get("Content-Type", "")
    if "multipart/form-data" in ctype:
        reader = await request.multipart()
        field = await reader.next()
        while field is not None and field.name != "file":
            field = await reader.next()
        if field is None:
            return _err("expected a 'file' field", 400)
        suffix = ".zip" if (field.filename or "").lower().endswith(".zip") else ".json"
        fd, tmp_name = tempfile.mkstemp(prefix="upload-", suffix=suffix, dir=str(_imports_dir()))
        tmp = Path(tmp_name)
        size = 0
        try:
            with open(fd, "wb") as out:
                while True:
                    chunk = await field.read_chunk(1 << 20)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise ValueError("upload too large")
                    out.write(chunk)
        except ValueError as exc:
            tmp.unlink(missing_ok=True)
            return _err(str(exc), 413)
        jid = _start_import_thread(tmp, delete_after=True)
        return _ok({"job": jid, "bytes": size}, 202)
    body = await _body(request)
    raw = str(body.get("path") or "").strip()
    if not raw:
        return _err("send a multipart 'file' or a JSON 'path'", 400)
    path = Path(raw).expanduser()
    if not path.exists():
        return _err("no such file on this computer", 404, code="not_found")
    return _ok({"job": _start_import_thread(path, delete_after=False)}, 202)


async def handle_import_status(request):
    job = await _run(get_store().get_import, request.match_info["job"])
    return _ok(job) if job else _err("no such import", 404, code="not_found")


# ── export ───────────────────────────────────────────────────────────────────


def _purge_tokens() -> None:
    now = time.monotonic()
    with _tokens_lock:
        for t in [t for t, v in _tokens.items() if v["expires"] < now]:
            _tokens.pop(t, None)


def _export_ids(store, body: dict[str, Any]) -> list[str]:
    if isinstance(body.get("ids"), list):
        return [str(i) for i in body["ids"]][:100000]
    collection = body.get("collection") or "all"
    folder = body.get("folder") or None
    ids: list[str] = []
    offset = 0
    while True:
        rows, total = store.list(folder=folder, collection=collection, sort="oldest", limit=1000, offset=offset)
        ids += [r["id"] for r in rows]
        offset += len(rows)
        if not rows or offset >= total:
            return ids


async def handle_export(request):
    """Mint a single-use download: body ``{format, ids? | folder? | collection?}`` -> ``{url}``."""
    from navig_chats.export import FORMATS

    body = await _body(request)
    fmt = str(body.get("format") or "md")
    if fmt not in (*FORMATS, "zip"):
        return _err(f"format must be one of {', '.join(FORMATS)} or zip", 400)
    ids = await _run(_export_ids, get_store(), body)
    if not ids:
        return _err("nothing to export", 400, code="empty")
    if fmt != "zip" and len(ids) > 1:
        fmt_note = "zip"  # several chats always travel as an archive
    else:
        fmt_note = fmt
    formats = body.get("formats") if isinstance(body.get("formats"), list) else None
    _purge_tokens()
    token = secrets.token_urlsafe(24)
    with _tokens_lock:
        _tokens[token] = {
            "ids": ids,
            "format": fmt_note,
            "inner": tuple(f for f in (formats or ([fmt] if fmt != "zip" else ["md", "json"])) if f in FORMATS) or ("md", "json"),
            "expires": time.monotonic() + EXPORT_TOKEN_TTL,
        }
    return _ok({"url": f"/api/deck/chats/export/download?token={token}", "count": len(ids), "format": fmt_note})


async def handle_export_download(request):
    from navig_chats import export as ex

    token = request.query.get("token", "")
    with _tokens_lock:
        job = _tokens.pop(token, None)
    if job is None or job["expires"] < time.monotonic():
        return _err("this download link has expired — export again", 410, code="expired")
    if "text" in job:  # a prepared document (a handoff pack)
        return web.Response(
            body=job["text"].encode("utf-8"),
            headers={"Content-Type": "text/markdown; charset=utf-8", "Content-Disposition": _disposition(job["filename"])},
        )
    store = get_store()
    if job["format"] != "zip":
        try:
            name, content = await _run(ex.export_one, store, job["ids"][0], job["format"])
        except LookupError:
            return _err("no such chat in the library", 404, code="not_found")
        ctype = {"md": "text/markdown", "txt": "text/plain", "html": "text/html", "json": "application/json"}[job["format"]]
        return web.Response(
            body=content.encode("utf-8"),
            headers={"Content-Type": f"{ctype}; charset=utf-8", "Content-Disposition": _disposition(name)},
        )
    tmp_dir = Path(tempfile.mkdtemp(prefix="chats-export-"))
    dest = tmp_dir / f"navig-chats-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    try:
        await _run(ex.write_zip, store, job["ids"], dest, formats=tuple(job["inner"]))
        resp = web.StreamResponse(headers={"Content-Type": "application/zip", "Content-Disposition": _disposition(dest.name)})
        resp.content_length = dest.stat().st_size
        await resp.prepare(request)
        with dest.open("rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                await resp.write(chunk)
        await resp.write_eof()
        return resp
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _disposition(name: str) -> str:
    from urllib.parse import quote

    ascii_name = name.encode("ascii", "replace").decode().replace('"', "'")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"


def _recall_enabled() -> bool | None:
    """Whether NAVIG's knowledge index reads local sources (None: core too old to say)."""
    try:
        from navig.config import get_config_manager
        from navig.core.coerce import coerce_bool
    except ImportError:
        return None
    try:
        return coerce_bool(get_config_manager().get("recall.read_local"), default=False)
    except Exception:  # noqa: BLE001 — an unreadable config is "unknown", never "on"
        return None


async def handle_brain(request):
    """What is shared with NAVIG's knowledge index, at which level, and whether it is reading."""
    from navig_chats.store import BRAIN_LEVELS, brain_shared

    rows = await _run(brain_shared, get_store())
    return _ok({
        "levels": list(BRAIN_LEVELS),
        "recall_enabled": _recall_enabled(),
        "shared": [{"id": r["id"], "title": r["display_title"], "level": r["brain_level"], "project": r["brain_project"]}
                   for r in rows],
    })


async def handle_extract(request):
    """Decisions, open questions/tasks, snippets and links found in one chat, each with its message id."""
    from navig_chats import extract as ex

    store = get_store()
    cid = request.match_info["id"]

    def build():
        conv = store.get(cid)
        if conv is None:
            return None
        items = ex.resolve(ex.extract(conv, store.messages(cid), conv.get("bookmarks")))
        return {"items": [i.to_dict() for i in items if i.kind != "link"], "counts": ex.summarize(items)}

    data = await _run(build)
    return _ok(data) if data is not None else _err("no such chat in the library", 404, code="not_found")


async def handle_handoff(request):
    """Continue this work: body ``{ids? | folder? | collection?, title?, goal?}`` -> the pack, plus a
    single-use download link for the Markdown file."""
    from navig_chats import handoff as ho

    body = await _body(request)
    store = get_store()
    ids = await _run(_export_ids, store, body)
    if not ids:
        return _err("nothing to hand off", 400, code="empty")
    try:
        pack = await _run(ho.build_handoff, store, ids, title=body.get("title") or None, goal=body.get("goal") or None)
    except LookupError as exc:
        return _err(str(exc), 404, code="not_found")
    _purge_tokens()
    token = secrets.token_urlsafe(24)
    with _tokens_lock:
        _tokens[token] = {"text": pack["markdown"], "filename": ho.pack_filename(pack),
                          "expires": time.monotonic() + EXPORT_TOKEN_TTL}
    return _ok({**pack, "filename": ho.pack_filename(pack), "url": f"/api/deck/chats/export/download?token={token}"})


async def handle_reindex(request):
    store = get_store()
    await _run(store.rebuild_index)
    return _ok({"intact": await _run(store.integrity)})


def register(app) -> None:
    """Mount the routes: module gate (403 when off) + this-computer-only. Static paths first."""
    from navig.modules.gate import requires_module

    g = requires_module("chats")

    def route(handler):
        return g(_local_only(handler))

    r = app.router
    r.add_get("/api/deck/chats", route(handle_list))
    r.add_get("/api/deck/chats/overview", route(handle_overview))
    r.add_get("/api/deck/chats/search", route(handle_search))
    r.add_post("/api/deck/chats/capture", route(handle_capture))
    r.add_post("/api/deck/chats/visit", route(handle_visit))
    r.add_post("/api/deck/chats/bulk", route(handle_bulk))
    r.add_get("/api/deck/chats/folders", route(handle_folders))
    r.add_post("/api/deck/chats/folders", route(handle_folder_create))
    r.add_patch("/api/deck/chats/folders/{fid}", route(handle_folder_patch))
    r.add_delete("/api/deck/chats/folders/{fid}", route(handle_folder_delete))
    r.add_post("/api/deck/chats/bookmarks", route(handle_bookmark_add))
    r.add_delete("/api/deck/chats/bookmarks/{bid}", route(handle_bookmark_delete))
    r.add_post("/api/deck/chats/import", route(handle_import))
    r.add_get("/api/deck/chats/import/{job}", route(handle_import_status))
    r.add_post("/api/deck/chats/export", route(handle_export))
    r.add_get("/api/deck/chats/export/download", route(handle_export_download))
    r.add_post("/api/deck/chats/reindex", route(handle_reindex))
    r.add_get("/api/deck/chats/brain", route(handle_brain))
    r.add_post("/api/deck/chats/handoff", route(handle_handoff))
    r.add_get("/api/deck/chats/{id}/extract", route(handle_extract))
    r.add_get("/api/deck/chats/{id}/messages", route(handle_messages))
    r.add_get("/api/deck/chats/{id}", route(handle_get))
    r.add_patch("/api/deck/chats/{id}", route(handle_patch))
    r.add_delete("/api/deck/chats/{id}", route(handle_delete))


__all__ = ["get_store", "register"]
