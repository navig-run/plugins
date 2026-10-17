"""Exports that stay readable without NAVIG — and a bundle NAVIG can restore from.

Formats: ``md`` (Markdown with YAML front matter), ``txt``, ``html`` (one self-contained file,
inline CSS, printable to PDF from any browser), ``json`` (the ``navig-chats`` bundle: every
field and every branch, re-importable with :func:`restore_bundle`) and ``zip`` (one file per
chat in its folder path, plus ``manifest.json`` with a sha256 per file and the attachments
the library never held).

Restoring a bundle restores the LIBRARY, not the provider: it cannot recreate a chat in
ChatGPT's own history.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import tempfile
import zipfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from navig_sdk.store import utcnow

from navig_chats.models import COMPLETENESS, PROVENANCE, ConversationIn, MessageIn

BUNDLE_FORMAT = "navig-chats"
BUNDLE_VERSION = 1
FORMATS = ("md", "txt", "html", "json")

_PROVIDER_LABEL = {"chatgpt": "ChatGPT", "claude": "Claude", "gemini": "Gemini", "grok": "Grok"}
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_FENCE_BLOCK = re.compile(r"```([\w+#.-]*)\n(.*?)(?:\n```|\Z)", re.DOTALL)


def role_label(role: str, provider: str) -> str:
    if role == "user":
        return "You"
    if role == "assistant":
        return _PROVIDER_LABEL.get(provider, "Assistant")
    return role.capitalize()


def safe_name(text: str, limit: int = 80) -> str:
    cleaned = _UNSAFE.sub(" ", text or "").strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)[:limit].strip()
    return cleaned or "untitled"


def filename(conv: dict[str, Any], ext: str, template: str = "{date}-{title}-{shortid}") -> str:
    stamp = conv.get("created_at") or conv.get("updated_at") or conv.get("captured_at") or ""
    date = stamp[:10] or "undated"
    short = re.sub(r"[^0-9a-zA-Z]", "", conv.get("native_id") or conv["id"])[:8]
    name = template.format(date=date, title=safe_name(conv.get("display_title") or ""), shortid=short)
    return f"{safe_name(name, 140)}.{ext}"


def folder_paths(folders: list[dict[str, Any]]) -> dict[str, str]:
    """``folder id -> "Parent/Child"`` for every folder."""
    by_id = {f["id"]: f for f in folders}
    out: dict[str, str] = {}
    for fid in by_id:
        parts, cur, guard = [], fid, 0
        while cur and cur in by_id and guard < 64:
            parts.append(safe_name(by_id[cur]["name"], 60))
            cur = by_id[cur]["parent_id"]
            guard += 1
        out[fid] = "/".join(reversed(parts))
    return out


def _yaml_scalar(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_yaml_scalar(x) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)  # a JSON string is a valid YAML scalar


def to_markdown(conv: dict[str, Any], messages: list[dict[str, Any]], folder_names: list[str] | None = None) -> str:
    front = {
        "id": conv["id"],
        "provider": conv["provider"],
        "title": conv.get("title"),
        "alias": conv.get("alias"),
        "url": conv.get("url"),
        "created": conv.get("created_at"),
        "updated": conv.get("updated_at"),
        "completeness": conv.get("completeness"),
        "provenance": conv.get("provenance"),
        "remote_project": conv.get("remote_project_id"),
        "folders": folder_names or [],
        "tags": conv.get("tags") or [],
        "messages": len(messages),
        "exported_at": utcnow(),
    }
    lines = ["---", *(f"{k}: {_yaml_scalar(v)}" for k, v in front.items()), "---", "", f"# {conv.get('display_title') or 'Untitled chat'}", ""]
    if conv.get("completeness") != "complete":
        lines += [f"> Library copy: **{conv.get('completeness')}** — it may not hold every message or branch.", ""]
    for m in messages:
        stamp = f" · {m['created_at'][:16].replace('T', ' ')}" if m.get("created_at") else ""
        lines += [f"## {role_label(m['role'], conv['provider'])}{stamp}", "", m["text"].rstrip(), ""]
        missing = [a for a in m.get("attachments") or [] if not a.get("available")]
        if missing:
            names = ", ".join(str(a.get("name") or a.get("kind") or "file") for a in missing)
            lines += [f"> Attachment not in the library: {names}", ""]
    return "\n".join(lines).rstrip() + "\n"


def to_text(conv: dict[str, Any], messages: list[dict[str, Any]]) -> str:
    out = [conv.get("display_title") or "Untitled chat", conv.get("url") or "", ""]
    for m in messages:
        out += [f"[{role_label(m['role'], conv['provider'])}]", m["text"].rstrip(), ""]
    return "\n".join(out).rstrip() + "\n"


def _html_body(text: str) -> str:
    """Code fences become <pre>; everything else is escaped paragraphs. No markup passes through."""
    out: list[str] = []
    pos = 0
    for m in _FENCE_BLOCK.finditer(text):
        before = text[pos : m.start()]
        out += [f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>" for p in re.split(r"\n{2,}", before) if p.strip()]
        lang = html.escape(m.group(1) or "")
        out.append(f'<pre data-lang="{lang}"><code>{html.escape(m.group(2))}</code></pre>')
        pos = m.end()
    out += [f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>" for p in re.split(r"\n{2,}", text[pos:]) if p.strip()]
    return "\n".join(out)


_CSS = """body{font:15px/1.6 system-ui,-apple-system,Segoe UI,sans-serif;max-width:860px;margin:2rem auto;padding:0 1rem;color:#1d1d1f;background:#fff}
header{border-bottom:1px solid #ddd;margin-bottom:1.5rem}h1{font-size:1.5rem;margin:.2rem 0}.meta{color:#666;font-size:.85rem}
.msg{margin:1.2rem 0;padding:.8rem 1rem;border-radius:8px;background:#f6f6f7}.msg.user{background:#eef5fb}
.role{font-weight:600;font-size:.8rem;text-transform:uppercase;letter-spacing:.04em;color:#555}
pre{background:#111;color:#eee;padding:.8rem;border-radius:6px;overflow:auto;font:13px/1.45 ui-monospace,Consolas,monospace}
.note{border-left:3px solid #c90;padding-left:.6rem;color:#664}
@media (prefers-color-scheme:dark){body{background:#111;color:#e6e6e6}.msg{background:#1c1c1e}.msg.user{background:#17222c}.meta,.role{color:#aaa}}
@media print{.msg{break-inside:avoid}}"""


def to_html(conv: dict[str, Any], messages: list[dict[str, Any]]) -> str:
    title = html.escape(conv.get("display_title") or "Untitled chat")
    meta = " · ".join(
        html.escape(x)
        for x in (conv.get("provider", ""), (conv.get("created_at") or "")[:10], conv.get("completeness", ""))
        if x
    )
    url = html.escape(conv.get("url") or "")
    parts = [
        "<!doctype html>", '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>{title}</title><style>{_CSS}</style></head><body>",
        f'<header><h1>{title}</h1><p class="meta">{meta}' + (f' · <a href="{url}">original</a>' if url else "") + "</p></header>",
    ]
    if conv.get("completeness") != "complete":
        parts.append(f'<p class="note">Library copy: {html.escape(conv.get("completeness", ""))} — it may not hold every message.</p>')
    for m in messages:
        role = html.escape(m["role"])
        parts.append(
            f'<section class="msg {role}" id="m-{html.escape(m["id"])}"><div class="role">'
            f"{html.escape(role_label(m['role'], conv['provider']))}</div>{_html_body(m['text'])}</section>"
        )
    parts.append("</body></html>")
    return "\n".join(parts)


def to_bundle(conv: dict[str, Any], messages: list[dict[str, Any]], folder_names: list[str] | None = None) -> dict[str, Any]:
    return {
        "format": BUNDLE_FORMAT,
        "version": BUNDLE_VERSION,
        "exported_at": utcnow(),
        "conversation": {**conv, "folder_paths": folder_names or []},
        "messages": messages,
    }


def render(fmt: str, conv: dict[str, Any], messages: list[dict[str, Any]], folder_names: list[str] | None = None) -> str:
    if fmt == "md":
        return to_markdown(conv, messages, folder_names)
    if fmt == "txt":
        return to_text(conv, messages)
    if fmt == "html":
        return to_html(conv, messages)
    if fmt == "json":
        return json.dumps(to_bundle(conv, messages, folder_names), ensure_ascii=False, indent=2)
    raise ValueError(f"unknown format {fmt!r}; use one of {', '.join(FORMATS)} or zip")


def _conv_folder_names(conv: dict[str, Any], paths: dict[str, str]) -> list[str]:
    return sorted(paths[f] for f in conv.get("folders") or [] if f in paths)


def export_one(store: Any, cid: str, fmt: str, *, branch: str = "active") -> tuple[str, str]:
    """(filename, content) for one chat."""
    conv = store.get(cid)
    if conv is None:
        raise LookupError(cid)
    paths = folder_paths(store.folders())
    msgs = store.messages(cid, branch="all" if fmt == "json" else branch)
    return filename(conv, fmt), render(fmt, conv, msgs, _conv_folder_names(conv, paths))


def write_zip(
    store: Any,
    ids: Iterable[str],
    dest: Path,
    *,
    formats: tuple[str, ...] = ("md", "json"),
    template: str = "{date}-{title}-{shortid}",
) -> dict[str, Any]:
    """Write a folder-aware archive to ``dest`` atomically; returns the manifest.

    One chat is read and written at a time, so a whole library never sits in memory.
    """
    for f in formats:
        if f not in FORMATS:
            raise ValueError(f"unknown format {f!r}")
    paths = folder_paths(store.folders())
    manifest: dict[str, Any] = {
        "format": f"{BUNDLE_FORMAT}-archive", "version": BUNDLE_VERSION, "created_at": utcnow(),
        "formats": list(formats), "files": [], "skipped_attachments": [], "missing": [],
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".chats-", suffix=".zip.part", dir=str(dest.parent))
    os.close(fd)
    used: set[str] = set()
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for cid in ids:
                conv = store.get(cid)
                if conv is None:
                    manifest["missing"].append(cid)
                    continue
                names = _conv_folder_names(conv, paths)
                folder = names[0] if names else "Unfiled"
                all_msgs = store.messages(cid, branch="all")
                active = [m for m in all_msgs if m["on_active_branch"]]
                for m in all_msgs:
                    for a in m.get("attachments") or []:
                        if not a.get("available"):
                            manifest["skipped_attachments"].append(
                                {"conversation_id": cid, "message_id": m["id"], "name": a.get("name"), "kind": a.get("kind")}
                            )
                for fmt in formats:
                    base = filename(conv, fmt, template)
                    arc = f"{folder}/{base}"
                    n = 2
                    while arc.lower() in used:
                        arc = f"{folder}/{base.rsplit('.', 1)[0]}-{n}.{fmt}"
                        n += 1
                    used.add(arc.lower())
                    data = render(fmt, conv, all_msgs if fmt == "json" else active, names).encode("utf-8")
                    zf.writestr(arc, data)
                    manifest["files"].append(
                        {"path": arc, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                         "conversation_id": cid, "format": fmt, "completeness": conv["completeness"]}
                    )
            manifest["count"] = len({f["conversation_id"] for f in manifest["files"]})
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            zf.writestr(
                "README.txt",
                "Exported from the NAVIG chat library.\n"
                "Each chat is a Markdown (.md) and/or JSON (.json) file inside its folder.\n"
                "manifest.json lists every file with its sha256. Attachments the library never held\n"
                "are listed under skipped_attachments. Re-import with: navig chats restore <this zip>\n",
            )
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return manifest


def verify_zip(path: Path) -> list[str]:
    """Paths whose bytes no longer match the manifest (empty = intact)."""
    with zipfile.ZipFile(path) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        return [
            f["path"] for f in manifest["files"] if hashlib.sha256(zf.read(f["path"])).hexdigest() != f["sha256"]
        ]


def _bundle_to_conversation(bundle: dict[str, Any]) -> tuple[ConversationIn, dict[str, Any]]:
    if bundle.get("format") != BUNDLE_FORMAT:
        raise ValueError("not a navig-chats bundle")
    c = bundle["conversation"]
    msgs = [
        MessageIn(
            id=m["id"], role=m["role"], text=m.get("text") or "", parent_id=m.get("parent_id"),
            content_type=m.get("content_type") or "text", created_at=m.get("created_at"), model=m.get("model"),
            has_code=bool(m.get("has_code")), on_active_branch=bool(m.get("on_active_branch", True)),
            parts=m.get("parts"), attachments=m.get("attachments") or [],
        )
        for m in sorted(bundle.get("messages") or [], key=lambda m: m.get("position") or 0)
    ]
    completeness = c.get("completeness") if c.get("completeness") in COMPLETENESS else "partial"
    provenance = c.get("provenance") if c.get("provenance") in PROVENANCE else "manual"
    conv = ConversationIn(
        provider=c["provider"], native_id=c["native_id"], title=c.get("title") or "", url=c.get("url") or "",
        messages=msgs, completeness=completeness, provenance=provenance, created_at=c.get("created_at"),
        updated_at=c.get("updated_at"), remote_project_id=c.get("remote_project_id"),
        remote_project_name=c.get("remote_project_name"), gizmo_id=c.get("gizmo_id"),
        archived_remote=bool(c.get("archived_remote")),
    )
    return conv, c


def restore_bundle(store: Any, path: Path, *, only: set[str] | None = None) -> dict[str, int]:
    """Re-import ``.json`` bundles (one file, or every bundle inside a NAVIG archive ZIP).

    Restores messages, alias and tags when the library has none of its own — a restore never
    overwrites the person's current organisation. ``only`` limits it to those chat ids.
    """
    counts = {"added": 0, "updated": 0, "unchanged": 0, "skipped": 0}

    def apply(raw: bytes) -> None:
        bundle = json.loads(raw)
        conv, meta = _bundle_to_conversation(bundle)
        if only and conv.id not in only:
            counts["skipped"] += 1
            return
        res = store.upsert(conv)
        counts[res["status"]] += 1
        current = store.get(conv.id) or {}
        patch: dict[str, Any] = {}
        if meta.get("alias") and not current.get("alias"):
            patch["alias"] = meta["alias"]
        if meta.get("tags") and not current.get("tags"):
            patch["tags"] = meta["tags"]
        if patch:
            store.update(conv.id, **patch)

    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if name.endswith(".json") and name != "manifest.json":
                    apply(zf.read(name))
    else:
        apply(path.read_bytes())
    return counts


__all__ = [
    "BUNDLE_FORMAT",
    "FORMATS",
    "export_one",
    "filename",
    "render",
    "restore_bundle",
    "safe_name",
    "to_bundle",
    "to_html",
    "to_markdown",
    "to_text",
    "verify_zip",
    "write_zip",
]
