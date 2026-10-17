"""Import ChatGPT's own account-data export ("Settings → Data controls → Export data").

The export is a ZIP holding ``conversations.json`` — a JSON array, one object per chat — or,
in larger exports, several numbered files (``conversations-000.json``, …). Each chat is a
TREE: ``mapping`` holds every node (every regenerated answer and edited prompt is a sibling
branch) and ``current_node`` is the leaf of the branch ChatGPT showed last.

What this keeps: every visible message of every branch, with the active branch marked; the
chat's own id, timestamps, custom-GPT id and, when the chat belongs to a ChatGPT Project, that
project's id (the export names no project, so ``remote_project_name`` stays empty). Hidden
system turns and custom-instruction context are skipped. Attachments are recorded as
metadata only — the export's ``file-…`` files are not copied, and each is listed with
``"available": false`` so nothing claims to hold a file it does not.

The array is read incrementally (:func:`iter_json_array`): a multi-hundred-megabyte export
never sits in memory whole. A chat the parser cannot make sense of is reported in the job's
errors and the rest still import.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any

from navig_sdk.store import utcnow

from navig_chats.models import ConversationIn, MessageIn

CHUNK = 1 << 20
_SPLIT_NAME = re.compile(r"(^|/)conversations(-\d+)?\.json$", re.IGNORECASE)
_FENCE = re.compile(r"^```", re.MULTILINE)


class ExportFormatError(ValueError):
    """The file is not a ChatGPT export (or its JSON is broken past recovery)."""


# ── streaming ────────────────────────────────────────────────────────────────


def iter_json_array(fp: IO[str]) -> Iterator[Any]:
    """Yield each element of a top-level JSON array, reading ``fp`` in chunks."""
    dec = json.JSONDecoder()
    buf = ""
    pos = 0
    eof = False

    def fill() -> bool:
        nonlocal buf, pos, eof
        chunk = fp.read(CHUNK)
        if not chunk:
            eof = True
            return False
        buf = buf[pos:] + chunk
        pos = 0
        return True

    def skip_ws() -> None:
        nonlocal pos
        while True:
            while pos < len(buf) and buf[pos] in " \t\r\n":
                pos += 1
            if pos < len(buf) or eof or not fill():
                return

    fill()
    if buf.startswith("﻿"):
        pos = 1
    skip_ws()
    if pos >= len(buf) or buf[pos] != "[":
        raise ExportFormatError("expected a JSON array of conversations")
    pos += 1
    first = True
    while True:
        skip_ws()
        if pos >= len(buf):
            raise ExportFormatError("the file ends before the conversation list does")
        if buf[pos] == "]":
            return
        if not first:
            if buf[pos] != ",":
                raise ExportFormatError(f"unexpected {buf[pos]!r} between conversations")
            pos += 1
            skip_ws()
        first = False
        while True:
            try:
                obj, end = dec.raw_decode(buf, pos)
            except json.JSONDecodeError as exc:
                if eof:
                    raise ExportFormatError(f"broken JSON in the export: {exc.msg}") from exc
                fill()
                continue
            if end >= len(buf) and not eof:
                # A number/literal may continue past the buffer: make sure it really ended.
                if fill():
                    continue
            pos = end
            yield obj
            break


# ── one conversation ─────────────────────────────────────────────────────────


def _iso(ts: Any) -> str | None:
    if ts in (None, "", 0):
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return str(ts) if isinstance(ts, str) else None


def _content_text(content: dict[str, Any]) -> tuple[str, bool, list[dict[str, Any]]]:
    """(searchable text, has code, attachment stubs) for one message's ``content``."""
    ctype = content.get("content_type") or "text"
    attachments: list[dict[str, Any]] = []
    if ctype == "code":
        lang = content.get("language") or ""
        if lang == "unknown":
            lang = ""
        return f"```{lang}\n{content.get('text') or ''}\n```", True, attachments
    if ctype == "execution_output":
        return f"```\n{content.get('text') or ''}\n```", True, attachments
    if ctype == "tether_quote":
        title = content.get("title") or ""
        url = content.get("url") or ""
        body = content.get("text") or ""
        return "\n".join(x for x in (f"> {title} {url}".strip(), body) if x), False, attachments
    if ctype == "tether_browsing_display":
        return str(content.get("result") or ""), False, attachments
    if ctype in ("thoughts",):
        parts = [t.get("content") or t.get("summary") or "" for t in content.get("thoughts") or [] if isinstance(t, dict)]
        return "\n\n".join(p for p in parts if p), False, attachments
    if ctype == "reasoning_recap":
        return str(content.get("content") or ""), False, attachments
    texts: list[str] = []
    for part in content.get("parts") or []:
        if isinstance(part, str):
            texts.append(part)
        elif isinstance(part, dict):
            kind = part.get("content_type") or "asset"
            if "asset_pointer" in part or kind.endswith("asset_pointer"):
                attachments.append(
                    {"kind": kind, "pointer": part.get("asset_pointer"), "available": False,
                     "width": part.get("width"), "height": part.get("height")}
                )
            elif isinstance(part.get("text"), str):
                texts.append(part["text"])
    if not texts and isinstance(content.get("text"), str):
        texts.append(content["text"])
    text = "\n".join(t for t in texts if t)
    return text, bool(_FENCE.search(text)), attachments


def _structured_parts(content: dict[str, Any]) -> list[Any] | None:
    """The raw ``parts`` only when they carry more than the text already holds (assets,
    structured blocks). Plain string parts are the text itself; storing them twice doubled
    the database."""
    parts = content.get("parts")
    if not isinstance(parts, list) or all(isinstance(p, str) for p in parts):
        return None
    return parts


def _active_chain(mapping: dict[str, Any], current: str | None) -> set[str]:
    chain: set[str] = set()
    node = current
    while node and node in mapping and node not in chain:
        chain.add(node)
        node = mapping[node].get("parent")
    return chain


def parse_conversation(raw: dict[str, Any], *, account_scope: str = "") -> ConversationIn:
    """One export object -> a :class:`ConversationIn`. Raises ``ValueError`` when unusable."""
    if not isinstance(raw, dict):
        raise ValueError("not a conversation object")
    native = raw.get("conversation_id") or raw.get("id")
    mapping = raw.get("mapping")
    if not native or not isinstance(mapping, dict):
        raise ValueError("missing id or message tree")
    current = raw.get("current_node")
    if current not in mapping:
        # No usable pointer: take the most recent leaf as the shown branch.
        leaves = [k for k, v in mapping.items() if isinstance(v, dict) and not v.get("children")]
        current = max(
            leaves,
            key=lambda k: float(((mapping[k].get("message") or {}).get("create_time")) or 0),
            default=None,
        )
    active = _active_chain(mapping, current)

    roots = [k for k, v in mapping.items() if isinstance(v, dict) and (v.get("parent") not in mapping)]
    order: list[str] = []
    seen: set[str] = set()
    stack = list(reversed(roots))
    while stack:  # depth-first, children oldest-first: ancestors always precede descendants
        nid = stack.pop()
        if nid in seen or nid not in mapping:
            continue
        seen.add(nid)
        order.append(nid)
        kids = [c for c in (mapping[nid].get("children") or []) if c in mapping]
        kids.sort(key=lambda c: float(((mapping[c].get("message") or {}).get("create_time")) or 0))
        stack.extend(reversed(kids))

    messages: list[MessageIn] = []
    visible_parent: dict[str, str | None] = {}
    for nid in order:
        node = mapping[nid]
        parent = node.get("parent")
        # Re-point each node at its nearest kept ancestor, so skipped nodes leave no gaps.
        nearest = visible_parent.get(parent) if parent in visible_parent else None
        msg = node.get("message")
        keep = False
        if isinstance(msg, dict):
            meta = msg.get("metadata") or {}
            role = ((msg.get("author") or {}).get("role")) or "assistant"
            content = msg.get("content") or {}
            ctype = content.get("content_type") or "text"
            hidden = bool(meta.get("is_visually_hidden_from_conversation")) or ctype in (
                "user_editable_context", "model_editable_context",
            )
            if not hidden and role in ("user", "assistant", "tool", "system"):
                text, has_code, attachments = _content_text(content)
                for a in meta.get("attachments") or []:
                    if isinstance(a, dict):
                        attachments.append(
                            {"kind": "file", "name": a.get("name"), "mime_type": a.get("mime_type"),
                             "size": a.get("size"), "available": False}
                        )
                if role == "system" and not text.strip():
                    hidden = True
                if not hidden and (text.strip() or attachments):
                    keep = True
                    messages.append(
                        MessageIn(
                            id=str(msg.get("id") or nid),
                            role=role,
                            text=text,
                            parent_id=nearest,
                            content_type=ctype,
                            created_at=_iso(msg.get("create_time")),
                            model=meta.get("model_slug"),
                            has_code=has_code,
                            on_active_branch=nid in active,
                            parts=_structured_parts(content),
                            attachments=attachments,
                        )
                    )
        visible_parent[nid] = str((msg or {}).get("id") or nid) if keep else nearest

    template = raw.get("conversation_template_id") or ""
    gizmo = raw.get("gizmo_id")
    is_project = isinstance(template, str) and template.startswith("g-p-")
    return ConversationIn(
        provider="chatgpt",
        native_id=str(native),
        title=str(raw.get("title") or "").strip(),
        url=f"https://chatgpt.com/c/{native}",
        messages=messages,
        completeness="complete",
        provenance="export",
        created_at=_iso(raw.get("create_time")),
        updated_at=_iso(raw.get("update_time")),
        account_scope=account_scope,
        remote_project_id=template if is_project else None,
        gizmo_id=gizmo if (gizmo and not is_project) else None,
        archived_remote=bool(raw.get("is_archived")),
    )


# ── sources ──────────────────────────────────────────────────────────────────


def _account_scope(user_json: bytes | str | None) -> str:
    if not user_json:
        return ""
    try:
        data = json.loads(user_json)
    except ValueError:
        return ""
    uid = data.get("id") if isinstance(data, dict) else None
    if not uid:
        return ""
    import hashlib  # noqa: PLC0415

    return "chatgpt-" + hashlib.sha256(str(uid).encode()).hexdigest()[:12]


def open_sources(path: Path) -> tuple[list[tuple[str, Callable[[], IO[str]]]], str, Callable[[], None]]:
    """The conversation files inside ``path`` (a ZIP, a folder or one JSON file), the account
    scope read from ``user.json`` when the export has one, and a ``close`` for the ZIP handle
    (Windows cannot delete an uploaded file while it is open)."""
    if not path.exists():
        raise FileNotFoundError(str(path))
    if path.is_dir():
        files = sorted(p for p in path.rglob("*.json") if _SPLIT_NAME.search(p.as_posix()))
        user = next(iter(path.rglob("user.json")), None)
        scope = _account_scope(user.read_text(encoding="utf-8")) if user else ""
        return [(p.name, (lambda p=p: p.open(encoding="utf-8"))) for p in files], scope, _noop
    if zipfile.is_zipfile(path):
        zf = zipfile.ZipFile(path)
        names = sorted(n for n in zf.namelist() if _SPLIT_NAME.search(n))
        scope = ""
        user = next((n for n in zf.namelist() if n.rsplit("/", 1)[-1] == "user.json"), None)
        if user:
            scope = _account_scope(zf.read(user))
        return [(n, (lambda n=n: io.TextIOWrapper(zf.open(n), encoding="utf-8"))) for n in names], scope, zf.close
    return [(path.name, (lambda: path.open(encoding="utf-8")))], "", _noop


def _noop() -> None:
    return None


def import_export(
    store: Any,
    path: Path,
    *,
    job_id: str | None = None,
    batch: int = 200,
    progress: Callable[[dict[str, Any]], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Import a ChatGPT export into ``store``. Idempotent: an unchanged chat is left alone.

    Commits every ``batch`` conversations, so an interruption keeps everything before it and a
    re-run skips those as ``unchanged``. Returns the job summary.
    """
    sources, scope, close = open_sources(path)
    try:
        return _run(store, sources, scope, str(path), job_id, batch, progress, cancelled)
    finally:
        close()


def _run(store, sources, scope, source, job_id, batch, progress, cancelled) -> dict[str, Any]:  # noqa: ANN001, PLR0913
    if not sources:
        raise ExportFormatError("no conversations.json in this export")
    jid = job_id or store.start_import(source)
    counts = {"total": 0, "done": 0, "added": 0, "updated": 0, "unchanged": 0}
    errors: list[dict[str, Any]] = []
    status = "done"

    def flush(pending: list[ConversationIn]) -> None:
        if not pending:
            return
        with store.transaction() as conn:
            for conv in pending:
                res = store.upsert(conv, conn=conn)
                counts[res["status"]] += 1
                counts["done"] += 1
        pending.clear()
        store.update_import(jid, **counts, errors=errors)
        if progress:
            progress({"job": jid, **counts, "errors": len(errors)})

    pending: list[ConversationIn] = []
    try:
        for name, opener in sources:
            with opener() as fp:
                for idx, raw in enumerate(iter_json_array(fp)):
                    if cancelled and cancelled():
                        status = "cancelled"
                        break
                    counts["total"] += 1
                    try:
                        pending.append(parse_conversation(raw, account_scope=scope))
                    except (ValueError, TypeError, KeyError, AttributeError) as exc:
                        counts["done"] += 1
                        title = raw.get("title") if isinstance(raw, dict) else None
                        errors.append({"file": name, "index": idx, "title": title, "error": str(exc)})
                    if len(pending) >= batch:
                        flush(pending)
            if status == "cancelled":
                break
        flush(pending)
    except ExportFormatError as exc:
        flush(pending)
        errors.append({"file": None, "index": None, "title": None, "error": str(exc)})
        if counts["done"] == 0:
            # Nothing was readable: this is not an export, not a failed import of one.
            store.update_import(jid, status="failed", errors=errors, finished_at=utcnow())
            raise
        status = "partial"
    except Exception:
        store.update_import(jid, status="failed", errors=errors, finished_at=utcnow())
        raise
    if status == "done" and errors:
        status = "partial"
    store.update_import(jid, status=status, **counts, errors=errors, finished_at=utcnow())
    return {"job": jid, "status": status, **counts, "errors": errors}


__all__ = ["ExportFormatError", "import_export", "iter_json_array", "open_sources", "parse_conversation"]
