"""`navig explore <folder>` — a universal local media/data explorer.

Point it at ANY folder (Downloads, a Telegram export, a drive dump). It walks the
tree, buckets every file by TYPE (image/gif/video/audio/document/text/archive/other),
auto-detects a Telegram export to pull in message text + captions + links, and serves
the full explorer UI: filter, preview, drag-to-class, extract texts, delete, commit.

Non-destructive: a ``.mediaexplorer/`` sidecar holds the index, your edits, thumbnails,
trash and exports. Originals are only moved when you commit ("Organize on disk"), into
``_ORGANIZED/<class>/`` (or a ``--out`` folder). Everything is reversible.

Stdlib only (+ optional Pillow/ffmpeg for thumbnails). Reuses ``explorer_app.html``.
"""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
from collections import Counter
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from navig.core.json_io import (
    JsonReadError,
    atomic_write_json,
    load_json_for_update,
    load_json_safe,
)

log = logging.getLogger(__name__)

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".heic", ".tiff"}
GIF_EXT = {".gif"}
VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".flv", ".wmv"}
AUDIO_EXT = {".mp3", ".m4a", ".ogg", ".oga", ".wav", ".opus", ".aac", ".flac", ".wma"}
TEXT_EXT = {".txt", ".md", ".srt", ".vtt", ".log", ".csv"}
DOC_EXT = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".epub", ".rtf", ".odt"}
ARCHIVE_EXT = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2"}
SKIP_DIRS = {".mediaexplorer", "_organized", ".trash", "$recycle.bin", "system volume information"}

try:
    from PIL import Image
    _HAVE_PIL = True
except Exception:  # noqa: BLE001
    _HAVE_PIL = False

# ── module state (set in serve) ─────────────────────────────────────────────
ROOT: Path = Path(".")
SIDE: Path = Path(".")
THUMBS: Path = Path(".")
TRASH: Path = Path(".")
EXPORTS: Path = Path(".")
OUT: Path = Path(".")
OVERRIDES: Path = Path(".")
HTML = Path(__file__).with_name("explorer_app.html")
LOCK = threading.RLock()
ITEMS: dict = {}
ORDER: list = []
KNOWN_CLASSES: set = set()
INDEXING: bool = False          # True while build_index() walks the tree
INDEX_SEEN: int = 0             # files seen so far (progress for the UI)
MAX_FILES: int = 400_000        # safety cap for enormous trees


def ftype(p: Path) -> str:
    e = p.suffix.lower()
    if e in GIF_EXT:
        return "gif"
    if e in IMAGE_EXT:
        return "image"
    if e in VIDEO_EXT:
        return "video"
    if e in AUDIO_EXT:
        return "audio"
    if e in TEXT_EXT:
        return "text"
    if e in DOC_EXT:
        return "document"
    if e in ARCHIVE_EXT:
        return "archive"
    return "other"


def abspath(it) -> Path:
    return ROOT / it["new_rel"]


def _folder_class(it) -> str:
    """Class implied by an item already sitting under _ORGANIZED/<class>/."""
    parts = Path(it["new_rel"]).parts
    if len(parts) >= 2 and parts[0].lower() == "_organized":
        return parts[1]
    return ""


def _safe_class(name: str) -> str:
    """A class name usable as ONE path segment under ``_ORGANIZED/`` — no path
    separators or ``..`` traversal, so committing can never escape the folder
    (which would break the non-destructive / reversible guarantee)."""
    name = re.sub(r"[\\/]+", "-", (name or "").strip())  # path separators → hyphen
    name = name.replace("..", "")                        # no traversal
    return name.strip(". ").strip()[:64]                 # no leading/trailing dots/spaces


def _unique_dst(dst: Path) -> Path:
    """*dst* if free, else the first ``name (N).ext`` that doesn't exist — so a
    move / commit never overwrites (silent data loss) or errors on a name clash."""
    if not dst.exists():
        return dst
    stem, suffix = dst.stem, dst.suffix
    i = 1
    while (cand := dst.with_name(f"{stem} ({i}){suffix}")).exists():
        i += 1
    return cand


def _load_jsonl(path: Path, key="name") -> dict:
    d = {}
    if path.exists():
        for line in path.open(encoding="utf-8"):
            try:
                r = json.loads(line)
                if r.get(key):
                    d[r[key]] = r
            except Exception:  # noqa: BLE001
                pass
    return d


def _load_meta_by_rel(path: Path) -> dict:
    """meta.jsonl (from `probe`) keyed by rel-path (== item id)."""
    d = {}
    if path.exists():
        for line in path.open(encoding="utf-8", errors="replace"):
            try:
                r = json.loads(line)
                if r.get("rel"):
                    d[r["rel"]] = r
            except Exception:  # noqa: BLE001
                pass
    return d


def _load_dupes(path: Path) -> dict:
    """dupes.jsonl (from `dedup`) → rel -> flag. exact beats near; keep marked."""
    flags: dict = {}
    if not path.exists():
        return flags
    for line in path.open(encoding="utf-8", errors="replace"):
        try:
            g = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        kind = g.get("kind", "")
        keep = g.get("keep")
        for rel in g.get("members", []):
            if kind == "exact":
                flags[rel] = "exact-keep" if rel == keep else "exact-dupe"
            elif not flags.get(rel, "").startswith("exact"):
                flags[rel] = "near"
    return flags


def _load_overrides() -> dict:
    """Read-only load for rendering/apply (see ``_publish``). Degrades to ``{}`` on
    ANY failure — a lock, corruption, an absent file — and never writes back, so it
    can safely mask a transient error. The MUTATING callers must NOT use this: they
    go through ``load_json_for_update`` so a failed read aborts the save instead of
    persisting an empty dict over every saved label/move/delete."""
    return load_json_safe(OVERRIDES, default={})


def _save_overrides(ov: dict):
    """Atomic write (temp-file + fsync + atomic replace) — a crash mid-write can't
    truncate overrides.json (which holds every label/move/delete; a truncated file
    loads as ``{}`` = work lost)."""
    atomic_write_json(ov, OVERRIDES)


def save_override(kind, key, val):
    with LOCK:
        try:
            # load-for-UPDATE: a transiently-locked / unreadable store raises here so
            # the save is ABORTED rather than wiping every prior override with a {}.
            ov = load_json_for_update(OVERRIDES, default={})
        except JsonReadError:
            log.warning(
                "overrides.json unreadable right now — skipping this %s save so saved "
                "labels/moves/deletes aren't wiped (retries on the next edit)",
                kind,
            )
            return
        if kind == "deleted":
            ov.setdefault("deleted", []).append(key)
        else:
            ov.setdefault(kind, {})[key] = val
        _save_overrides(ov)


def register_class(name):
    if not name:
        return
    with LOCK:
        if name in KNOWN_CLASSES:
            return
        try:
            ov = load_json_for_update(OVERRIDES, default={})
        except JsonReadError:
            # Abort BEFORE marking the class known, so the next call retries instead
            # of leaving it registered in-memory but never persisted.
            log.warning(
                "overrides.json unreadable right now — skipping custom-class "
                "registration for %r (retries on the next edit)",
                name,
            )
            return
        KNOWN_CLASSES.add(name)
        cc = ov.setdefault("custom_classes", [])
        if name not in cc:
            cc.append(name)
            _save_overrides(ov)


def _telegram_captions() -> dict:
    """If ROOT is a Telegram HTML export, map media basename -> {captions, links}."""
    pages = sorted(ROOT.glob("messages*.html"))
    if not pages:
        return {}
    try:
        from bs4 import BeautifulSoup  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return {}
    out: dict = {}
    dirs = ("photos/", "video_files/", "files/")
    for page in pages:
        soup = BeautifulSoup(page.read_text(encoding="utf-8"), "html.parser")
        for msg in soup.select("div.message"):
            td = msg.select_one(".text")
            cap = td.get_text(" ", strip=True) if td else ""
            links = [a["href"] for a in (td.find_all("a", href=True) if td else []) if a["href"].startswith("http")]
            for a in msg.find_all("a", href=True):
                href = a["href"]
                if href.startswith(dirs):
                    n = os.path.basename(href)
                    e = out.setdefault(n, {"captions": [], "links": []})
                    if cap:
                        e["captions"].append(cap)
                    e["links"].extend(links)
    return out


def _publish(items: dict, order: list, *, done: bool):
    """Swap the live view. Partial (done=False) skips overrides; final applies them."""
    global ITEMS, ORDER, KNOWN_CLASSES
    if done:
        ov = _load_overrides()
        for _id in ov.get("deleted", []):
            items.pop(_id, None)
        for _id, nr in ov.get("moved", {}).items():
            if _id in items:
                items[_id]["new_rel"] = nr
        labels = ov.get("labels", {})
        for _id, kl in labels.items():
            if _id in items:
                items[_id]["klass"] = kl
        for _id, tl in ov.get("tags", {}).items():   # people/object/theme tags
            if _id in items:
                items[_id]["tags"] = tl
        for it in items.values():
            fc = _folder_class(it)
            it["filed"] = (not it["klass"]) or it["klass"] == fc
            it["pending"] = it["id"] in labels and it["klass"] != fc
        order = [i for i in order if i in items]
        kc = set(ov.get("custom_classes", [])) | {it["klass"] for it in items.values() if it["klass"]}
    else:
        kc = {it["klass"] for it in items.values() if it["klass"]}
    with LOCK:
        ITEMS, ORDER, KNOWN_CLASSES = dict(items), list(order), kc


def build_index():
    """Walk ROOT, bucket files by type. Robust to permission errors / huge trees;
    publishes partial results as it goes so the UI fills live. Sets INDEXING flags."""
    global INDEXING, INDEX_SEEN
    INDEXING, INDEX_SEEN = True, 0
    caps = _telegram_captions()
    tr = _load_jsonl(SIDE / "transcripts.jsonl")
    oc = _load_jsonl(SIDE / "ocr.jsonl")
    cls = _load_jsonl(SIDE / "classes.jsonl")
    meta = _load_meta_by_rel(SIDE / "meta.jsonl")   # probe: w/h/codec/date/gps/class
    dupes = _load_dupes(SIDE / "dupes.jsonl")        # dedup: exact/near flags
    items, order, seen = {}, [], set()
    try:
        for dirpath, dirnames, filenames in os.walk(ROOT, onerror=lambda e: None, followlinks=False):
            dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS and not d.startswith(".")]
            for fn in filenames:
                if INDEX_SEEN >= MAX_FILES:
                    print(f"  [explore] hit {MAX_FILES} files — stopping walk (folder too large)", flush=True)
                    break
                p = Path(dirpath) / fn
                try:
                    rel = p.relative_to(ROOT).as_posix()
                    st = p.stat()
                    size = st.st_size
                except Exception:  # noqa: BLE001 — permission / placeholder / vanished
                    continue
                INDEX_SEEN += 1
                _id = rel
                if _id in seen:
                    continue
                seen.add(_id)
                typ = ftype(p)
                cap = caps.get(fn, {})
                e = cls.get(fn, {})
                t = tr.get(fn, {})
                o = oc.get(fn, {})
                klass = _folder_class({"new_rel": rel}) or e.get("klass", "")
                text = ""
                # only read small text files that aren't reparse points (skip OneDrive placeholders)
                if typ == "text" and size < 200_000 and not _is_reparse(st):
                    try:
                        text = p.read_text(encoding="utf-8", errors="replace")[:5000]
                    except Exception:  # noqa: BLE001
                        text = ""
                blob = " ".join([fn, " ".join(cap.get("captions", [])),
                                 t.get("text", "")[:400], "\n".join(o.get("ocr_lines", []))[:200],
                                 text[:400]]).lower()
                m = meta.get(rel, {})
                items[_id] = {
                    "id": _id, "name": fn, "type": typ, "new_rel": rel, "klass": klass,
                    "bucket": "", "conf": e.get("confidence", 0), "size": size,
                    "dur": m.get("dur") or t.get("dur", 0), "lang": t.get("lang", ""),
                    # ── probe metadata (media consolidation) ──
                    "w": m.get("w", 0), "h": m.get("h", 0), "codec": m.get("codec", ""),
                    "fps": m.get("fps", 0), "res_tier": m.get("res_tier", ""),
                    "source_class": m.get("source_class", ""), "camera": m.get("camera", ""),
                    "created": m.get("created", ""), "year": m.get("year"),
                    "gps": m.get("gps"), "dupe": dupes.get(rel, ""), "tags": [],
                    "title": (cap.get("captions") or [os.path.splitext(fn)[0]])[0][:70],
                    "point": (e.get("point") or (cap.get("captions") or [""])[0])[:200],
                    "repos": e.get("repos", []), "links": cap.get("links", []),
                    "caption": " ".join(cap.get("captions", [])),
                    "transcript": t.get("text", ""), "ocr": "\n".join(o.get("ocr_lines", [])),
                    "content": text, "filed": True, "pending": False,
                    "has_media": typ in ("image", "gif", "video"),
                    "_blob": blob,
                }
                order.append(_id)
                if len(items) % 4000 == 0:          # live partial refresh
                    _publish(items, order, done=False)
            if INDEX_SEEN >= MAX_FILES:
                break
    finally:
        _publish(items, order, done=True)
        INDEXING = False
    print(f"  indexed {len(items)} files "
          f"({dict(Counter(i['type'] for i in items.values()))})", flush=True)


def _is_reparse(st) -> bool:
    """True for Windows reparse points (symlinks / OneDrive placeholders)."""
    return bool(getattr(st, "st_file_attributes", 0) & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def thumb_path(_id):
    return THUMBS / (hashlib.md5(_id.encode("utf-8")).hexdigest() + ".jpg")


def make_thumb(it, dst) -> bool:
    src = abspath(it)
    if not src.exists():
        return False
    try:
        if it["type"] in ("image", "gif") and _HAVE_PIL:
            im = Image.open(src).convert("RGB")
            im.thumbnail((360, 360))
            im.save(dst, "JPEG", quality=82)
            return True
        if it["type"] == "video":
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "1", "-i", str(src),
                            "-frames:v", "1", "-vf", "scale=360:-2", str(dst)],
                           timeout=40, capture_output=True)
            return dst.exists()
    except Exception:  # noqa: BLE001
        return False
    return False


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", ctype="application/json", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json")

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        p = u.path
        if p in ("/", "/index.html"):
            return self.serve_file(HTML, "text/html; charset=utf-8")
        if p == "/api/index":
            with LOCK:
                light = [{**{k: it.get(k) for k in (
                          "id", "type", "klass", "bucket", "conf", "size",
                          "dur", "lang", "title", "point", "repos", "links", "has_media",
                          "pending", "filed", "_blob",
                          "w", "h", "res_tier", "source_class", "year", "dupe",
                          "codec", "tags")},
                          "has_gps": bool(it.get("gps"))}
                         for it in (ITEMS[i] for i in ORDER)]
                classes = sorted(KNOWN_CLASSES)
            return self._json({"items": light, "classes": classes, "video_classes": [], "buckets": [],
                               "indexing": INDEXING, "seen": INDEX_SEEN})
        if p == "/api/item":
            with LOCK:
                it = ITEMS.get(q.get("id", [""])[0])
            return self._json({k: v for k, v in it.items() if k != "_blob"} if it else {"error": "not found"},
                              200 if it else 404)
        if p == "/api/stats":
            with LOCK:
                its = list(ITEMS.values())
            return self._json({"total": len(its), "by_type": dict(Counter(i["type"] for i in its)),
                               "by_class": dict(Counter(i["klass"] for i in its if i["klass"])),
                               "by_bucket": {}, "gb": round(sum(i["size"] for i in its) / (1024**3), 2),
                               "indexing": INDEXING, "seen": INDEX_SEEN})
        if p == "/thumb":
            return self.serve_thumb(q.get("id", [""])[0])
        if p == "/media":
            return self.serve_media(q.get("id", [""])[0])
        if p == "/download":
            return self.serve_file(EXPORTS / os.path.basename(q.get("f", [""])[0]),
                                   "application/octet-stream", download=True)
        return self._send(404, "not found", "text/plain")

    def serve_thumb(self, _id):
        with LOCK:
            it = ITEMS.get(_id)
        if not it or not it["has_media"]:
            return self._send(204)
        tp = thumb_path(_id)
        if not tp.exists() and not make_thumb(it, tp):
            return self._send(204)
        return self.serve_file(tp, "image/jpeg", cache=True)

    def serve_media(self, _id):
        with LOCK:
            it = ITEMS.get(_id)
        if not it:
            return self._send(404, "x", "text/plain")
        path = abspath(it)
        if not path.exists():
            return self._send(404, "missing", "text/plain")
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        return self.serve_file(path, ctype, ranged=True)

    def serve_file(self, path: Path, ctype, ranged=False, cache=False, download=False):
        path = Path(path)
        if not path.exists():
            return self._send(404, "not found", "text/plain")
        size = path.stat().st_size
        extra = {}
        if cache:
            extra["Cache-Control"] = "max-age=86400"
        if download:
            extra["Content-Disposition"] = f'attachment; filename="{path.name}"'
        rng = self.headers.get("Range")
        if ranged and rng and rng.startswith("bytes="):
            try:
                s, e = rng[6:].split("-")
                start = int(s) if s else 0
                end = min(int(e) if e else size - 1, size - 1)
                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length", str(end - start + 1))
                self.end_headers()
                with open(path, "rb") as f:
                    f.seek(start)
                    rem = end - start + 1
                    while rem > 0:
                        chunk = f.read(min(65536, rem))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        rem -= len(chunk)
                return
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception:  # noqa: BLE001
                pass
        try:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            if ranged:
                self.send_header("Accept-Ranges", "bytes")
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                with open(path, "rb") as f:
                    shutil.copyfileobj(f, self.wfile, 65536)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:  # noqa: BLE001
            body = {}
        ids = body.get("ids", [])
        if u.path == "/api/label":
            return self.op_label(ids, body.get("to", ""))
        if u.path == "/api/commit":
            return self.op_commit()
        if u.path == "/api/delete":
            return self.op_delete(ids)
        if u.path == "/api/tag":
            return self.op_tag(ids, body.get("tags", []))
        if u.path == "/api/export-text":
            return self.op_export_text(ids)
        if u.path == "/api/export":
            return self.op_export(ids, body.get("format", "csv"), body.get("copy_files", False))
        return self._send(404, "x", "text/plain")

    def op_label(self, ids, to):
        to = _safe_class(to)
        if not to:
            return self._json({"error": "no class"}, 400)
        n = 0
        for _id in ids:
            with LOCK:
                it = ITEMS.get(_id)
            if not it:
                continue
            with LOCK:
                it["klass"] = to
                it["filed"] = False
                it["pending"] = to != _folder_class(it)
            save_override("labels", _id, to)
            n += 1
        register_class(to)
        self._json({"labeled": n, "klass": to})

    def op_tag(self, ids, tags):
        """Set free-form people/object/theme tags on items (persisted, non-destructive)."""
        tags = [str(t).strip()[:40] for t in tags if str(t).strip()][:20]
        n = 0
        for _id in ids:
            with LOCK:
                it = ITEMS.get(_id)
            if not it:
                continue
            with LOCK:
                it["tags"] = tags
            save_override("tags", _id, tags)
            n += 1
        self._json({"tagged": n, "tags": tags})

    def op_commit(self):
        moved, errors = 0, 0
        with LOCK:
            todo = [it for it in ITEMS.values() if it["klass"] and not it["filed"]]
        for it in todo:
            src = abspath(it)
            # _safe_class again (defence in depth: legacy labels from before the
            # sanitizer could still carry path separators / ``..``).
            base = ROOT / "_ORGANIZED" / _safe_class(it["klass"]) / it["name"]
            try:
                base.parent.mkdir(parents=True, exist_ok=True)
                dst = base if src == base else _unique_dst(base)  # never overwrite a clash
                if src.exists() and src != dst:
                    shutil.move(str(src), str(dst))
                new_rel = dst.relative_to(ROOT).as_posix()
                with LOCK:
                    it["new_rel"] = new_rel
                    it["filed"] = True
                    it["pending"] = False
                save_override("moved", it["id"], new_rel)
                moved += 1
            except Exception:  # noqa: BLE001
                errors += 1
        self._json({"committed": moved, "errors": errors})

    def op_delete(self, ids):
        n = 0
        for _id in ids:
            with LOCK:
                it = ITEMS.get(_id)
            if not it:
                continue
            src = abspath(it)
            if src.exists():
                dst = _unique_dst(TRASH / it["name"])  # never clobber an existing trash file
                try:
                    shutil.move(str(src), str(dst))
                except Exception:  # noqa: BLE001
                    continue
            save_override("deleted", _id, True)
            with LOCK:
                ITEMS.pop(_id, None)
            n += 1
        with LOCK:
            global ORDER
            ORDER = [i for i in ORDER if i in ITEMS]
        self._json({"deleted": n, "trash": str(TRASH)})

    def op_export_text(self, ids):
        with LOCK:
            rows = [ITEMS[i] for i in (ids or ORDER) if i in ITEMS]
        ts = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        files = {}

        def dump(fn, lines):
            if lines:
                (EXPORTS / fn).write_text("\n".join(lines), encoding="utf-8")
                files[fn] = f"/download?f={urllib.parse.quote(fn)}"

        repos, links = set(), set()
        for r in rows:
            repos.update(r.get("repos", []))
            links.update(u for u in r.get("links", []) if u not in r.get("repos", []))
        dump(f"links-{ts}.md", ["# Links & repos\n"] + [f"- {u}" for u in sorted(repos | links)])
        dump(f"transcripts-{ts}.md", ["# Transcripts\n"] + [f"## {r['name']}\n{r['transcript']}\n"
                                                            for r in rows if r.get("transcript")])
        dump(f"texts-{ts}.md", ["# Texts / captions / OCR\n"] + [
            f"## {r['name']}\n{r.get('caption') or r.get('content') or r.get('ocr')}\n"
            for r in rows if (r.get("caption") or r.get("content") or r.get("ocr"))])
        self._json({"files": files, "items": len(rows), "repos": len(repos), "links": len(links)})

    def op_export(self, ids, fmt, copy_files):
        with LOCK:
            rows = [ITEMS[i] for i in (ids or ORDER) if i in ITEMS]
        ts = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        fn = f"export-{ts}.csv"
        with (EXPORTS / fn).open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["name", "type", "klass", "size", "new_rel", "title"])
            for r in rows:
                w.writerow([r["name"], r["type"], r["klass"], r["size"], r["new_rel"], r["title"]])
        copied = 0
        if copy_files:
            dest = EXPORTS / f"files-{ts}"
            dest.mkdir(parents=True, exist_ok=True)
            for r in rows:
                s = abspath(r)
                if s.exists():
                    try:
                        shutil.copy2(s, dest / r["name"])
                        copied += 1
                    except Exception:  # noqa: BLE001
                        pass
        self._json({"file": fn, "rows": len(rows), "copied_files": copied,
                    "download": f"/download?f={urllib.parse.quote(fn)}"})


def serve(root: Path, out: Path | None = None, port: int | None = None, open_browser: bool = True):
    """Serve the explorer UI. ``port=None`` prefers 8770 and falls back to a free port —
    Windows reserves whole port ranges, and 8770 sits inside one on some machines."""
    global ROOT, SIDE, THUMBS, TRASH, EXPORTS, OUT, OVERRIDES
    ROOT = Path(root).resolve()
    SIDE = ROOT / ".mediaexplorer"
    THUMBS, TRASH, EXPORTS = SIDE / "thumbs", SIDE / "trash", SIDE / "exports"
    OUT = Path(out).resolve() if out else ROOT / "_ORGANIZED"
    OVERRIDES = SIDE / "overrides.json"
    for d in (SIDE, THUMBS, TRASH, EXPORTS):
        d.mkdir(parents=True, exist_ok=True)
    # Bind the socket FIRST so the browser can connect immediately, THEN index in
    # the background (the UI shows "indexing… N" and fills live as files are found).
    from navig.http_bind import bind_http_server  # noqa: PLC0415
    srv, port = bind_http_server(H, port, preferred=8770)
    threading.Thread(target=build_index, name="explore-index", daemon=True).start()
    print(f"\n  Explorer ready → http://localhost:{port}   (indexing in background… Ctrl+C to stop)\n",
          flush=True)
    if open_browser:
        try:
            import webbrowser  # noqa: PLC0415
            webbrowser.open(f"http://localhost:{port}")
        except Exception:  # noqa: BLE001
            pass
    srv.serve_forever()
