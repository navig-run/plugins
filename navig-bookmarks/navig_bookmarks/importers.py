"""Read bookmarks from browser profiles and export files.

Sources: Netscape HTML (every browser's export), JSON (a list of ``{url, title, notes,
tags}`` or this plugin's own ``export --format json``), and the browsers' own files —
Chrome / Edge ``Bookmarks`` JSON, Firefox ``places.sqlite``, Safari ``Bookmarks.plist``.
Ported from navig core's ``navig.importers`` with three changes: folder names come from the
browser's own root names ("Bookmarks bar", not ``bookmark_bar``), Firefox is read from a
private copy (a running Firefox holds a lock on the live file), and Firefox tags become tags
instead of duplicate bookmarks under a ``tags/…`` folder.
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from navig_bookmarks import netscape
from navig_bookmarks.models import ParsedBookmark, join_folder, split_folder

SOURCES = ("html", "json", "chrome", "edge", "firefox", "safari")
BROWSERS = ("chrome", "edge", "firefox", "safari")


class ImportSourceError(ValueError):
    """The source could not be read or recognised; the message says why."""


# ── default locations ──────────────────────────────────────────────────────────


def default_path(source: str) -> Path | None:
    """The browser's default-profile bookmark file on this OS, if it exists."""
    home = Path.home()
    candidates: list[Path] = []
    if source in ("chrome", "edge"):
        vendor = ("Google", "Chrome") if source == "chrome" else ("Microsoft", "Edge")
        if sys.platform.startswith("win"):
            local = os.environ.get("LOCALAPPDATA")
            if local:
                candidates.append(Path(local, *vendor, "User Data", "Default", "Bookmarks"))
        elif sys.platform == "darwin":
            app = "Google/Chrome" if source == "chrome" else "Microsoft Edge"
            candidates.append(home / "Library" / "Application Support" / app / "Default" / "Bookmarks")
        else:
            app = "google-chrome" if source == "chrome" else "microsoft-edge"
            candidates.append(home / ".config" / app / "Default" / "Bookmarks")
    elif source == "safari":
        candidates.append(home / "Library" / "Safari" / "Bookmarks.plist")
    elif source == "firefox":
        found = _firefox_places()
        if found:
            candidates.append(found)
    for c in candidates:
        if c.exists():
            return c
    return None


def _firefox_places() -> Path | None:
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return None
        ini = Path(appdata) / "Mozilla" / "Firefox" / "profiles.ini"
    elif sys.platform == "darwin":
        ini = Path.home() / "Library" / "Application Support" / "Firefox" / "profiles.ini"
    else:
        ini = Path.home() / ".mozilla" / "firefox" / "profiles.ini"
    if not ini.exists():
        return None
    profiles: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for raw in ini.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            if current:
                profiles.append(current)
            current = {}
        elif "=" in line:
            k, v = line.split("=", 1)
            current[k.strip()] = v.strip()
    if current:
        profiles.append(current)
    profiles = [p for p in profiles if p.get("Path")]
    if not profiles:
        return None
    chosen = next((p for p in profiles if p.get("Default") == "1"), profiles[0])
    base = ini.parent / chosen["Path"] if chosen.get("IsRelative", "1") == "1" else Path(chosen["Path"])
    places = base / "places.sqlite"
    return places if places.exists() else None


# ── detection ──────────────────────────────────────────────────────────────────


def detect_source(path: Path) -> str:
    """Which reader fits *path*. Raises :class:`ImportSourceError` when none does."""
    name = path.name.lower()
    if path.is_dir() and (path / "places.sqlite").exists():
        return "firefox"
    if name.endswith("places.sqlite"):
        return "firefox"
    if name.endswith(".plist"):
        return "safari"
    if name.endswith((".html", ".htm")):
        return "html"
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise ImportSourceError(f"cannot read {path}: {exc}") from exc
    if netscape.looks_like_netscape(text):
        return "html"
    try:
        payload = json.loads(text)
    except ValueError:
        raise ImportSourceError(
            f"{path.name} is not a bookmark export this tool recognises "
            "(Netscape HTML, JSON, Chrome/Edge Bookmarks, places.sqlite, Bookmarks.plist)"
        ) from None
    if isinstance(payload, dict) and isinstance(payload.get("roots"), dict):
        return "edge" if ("edge" in str(path).lower() or "microsoft" in str(path).lower()) else "chrome"
    return "json"


# ── readers ────────────────────────────────────────────────────────────────────


def read(source: str, path: Path) -> list[ParsedBookmark]:
    """Every bookmark in *path*, read as *source*."""
    if not path.exists():
        raise ImportSourceError(f"not found: {path}")
    if source == "html":
        return netscape.parse(path.read_text(encoding="utf-8-sig", errors="replace"))
    if source == "json":
        return _read_json(path)
    if source in ("chrome", "edge"):
        return _read_chromium(path)
    if source == "firefox":
        return _read_firefox(path / "places.sqlite" if path.is_dir() else path)
    if source == "safari":
        return _read_safari(path)
    raise ImportSourceError(f"unknown source {source!r}; one of: {', '.join(SOURCES)}")


def _read_json(path: Path) -> list[ParsedBookmark]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ImportSourceError(f"{path.name}: not valid JSON ({exc})") from exc
    rows = payload.get("bookmarks") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ImportSourceError(f"{path.name}: expected a list of bookmarks or {{\"bookmarks\": [...]}}")
    out: list[ParsedBookmark] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("url"):
            continue
        raw_tags = row.get("tags") or []
        if isinstance(raw_tags, str):
            raw_tags = raw_tags.split(",")
        tags = []
        for t in raw_tags:
            name = t.get("tag") if isinstance(t, dict) else t
            if name and str(name).strip():
                tags.append(str(name).strip())
        out.append(
            ParsedBookmark(
                url=str(row["url"]).strip(),
                title=str(row.get("title") or ""),
                notes=str(row.get("notes") or ""),
                folder=str(row.get("folder") or row.get("folder_path") or ""),
                tags=tags,
            )
        )
    return out


def _read_chromium(path: Path) -> list[ParsedBookmark]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ImportSourceError(f"{path.name}: not a Chrome/Edge Bookmarks file ({exc})") from exc
    roots = payload.get("roots") if isinstance(payload, dict) else None
    if not isinstance(roots, dict):
        raise ImportSourceError(f"{path.name}: no 'roots' — not a Chrome/Edge Bookmarks file")
    friendly = {"bookmark_bar": "Bookmarks bar", "other": "Other bookmarks", "synced": "Mobile bookmarks"}
    out: list[ParsedBookmark] = []
    for key in ("bookmark_bar", "other", "synced"):
        node = roots.get(key)
        if isinstance(node, dict):
            root_name = str(node.get("name") or friendly[key])
            for child in node.get("children", []):
                _walk_chromium(child, [root_name], out)
    return out


def _walk_chromium(node: dict, folder: list[str], out: list[ParsedBookmark]) -> None:
    kind = node.get("type")
    if kind == "folder":
        name = str(node.get("name") or "Untitled")
        for child in node.get("children", []):
            _walk_chromium(child, [*folder, name], out)
    elif kind == "url" and node.get("url"):
        added = None
        raw = str(node.get("date_added") or "")
        if raw.isdigit() and int(raw) > 11644473600000000:
            # Chromium time: microseconds since 1601-01-01.
            added = int(raw) // 1_000_000 - 11644473600
        out.append(
            ParsedBookmark(url=str(node["url"]), title=str(node.get("name") or ""), folder=join_folder(folder), added=added)
        )


_FIREFOX_ROOTS = {
    "menu________": "Bookmarks Menu",
    "toolbar_____": "Bookmarks Toolbar",
    "unfiled_____": "Other Bookmarks",
    "mobile______": "Mobile Bookmarks",
}


def _read_firefox(places: Path) -> list[ParsedBookmark]:
    # A running Firefox keeps places.sqlite locked (and holds recent writes in -wal), so read
    # a private copy of the database and its WAL. The copy is ours alone: nothing can lock it.
    with tempfile.TemporaryDirectory(prefix="navig-bookmarks-ff-") as tmp:
        copy = Path(tmp) / "places.sqlite"
        try:
            shutil.copy2(places, copy)
            for suffix in ("-wal", "-shm"):
                side = places.with_name(places.name + suffix)
                if side.exists():
                    shutil.copy2(side, copy.with_name(copy.name + suffix))
        except OSError as exc:
            raise ImportSourceError(f"cannot copy {places}: {exc}") from exc
        con = sqlite3.connect(str(copy), timeout=5)
        try:
            con.row_factory = sqlite3.Row
            return _firefox_rows(con)
        except sqlite3.DatabaseError as exc:
            raise ImportSourceError(f"{places.name}: not a readable Firefox places database ({exc})") from exc
        finally:
            con.close()


def _firefox_rows(con: sqlite3.Connection) -> list[ParsedBookmark]:
    folders: dict[int, dict] = {}
    for row in con.execute("SELECT id, parent, title, guid FROM moz_bookmarks WHERE type = 2"):
        folders[row["id"]] = {"title": row["title"] or "", "parent": row["parent"], "guid": row["guid"] or ""}
    tags_root = next((fid for fid, f in folders.items() if f["guid"] == "tags________"), None)
    tag_folders = {fid: f["title"] for fid, f in folders.items() if tags_root is not None and f["parent"] == tags_root}

    tags_by_place: dict[int, list[str]] = {}
    out: list[tuple[int, ParsedBookmark]] = []
    rows = con.execute(
        """SELECT b.id, b.parent, b.fk, b.dateAdded AS added,
                  COALESCE(b.title, p.title, '') AS title, p.url
           FROM moz_bookmarks b JOIN moz_places p ON b.fk = p.id
           WHERE b.type = 1 ORDER BY b.parent, b.position"""
    )
    for row in rows:
        url = row["url"] or ""
        if row["parent"] in tag_folders:
            tags_by_place.setdefault(row["fk"], []).append(tag_folders[row["parent"]])
            continue
        if not url or url.startswith("place:"):
            continue
        chain: list[str] = []
        seen: set[int] = set()
        cur = row["parent"]
        while cur in folders and cur not in seen:
            seen.add(cur)
            f = folders[cur]
            name = _FIREFOX_ROOTS.get(f["guid"]) or f["title"]
            if name and f["guid"] != "root________":
                chain.append(name)
            cur = f["parent"]
        chain.reverse()
        added = int(row["added"]) // 1_000_000 if row["added"] else None  # microseconds
        out.append((row["fk"], ParsedBookmark(url=url, title=row["title"] or "", folder=join_folder(chain), added=added)))
    for fk, bm in out:
        bm.tags = sorted(set(tags_by_place.get(fk, [])))
    return [bm for _, bm in out]


def _read_safari(path: Path) -> list[ParsedBookmark]:
    try:
        with path.open("rb") as fh:
            payload = plistlib.load(fh)
    except (OSError, plistlib.InvalidFileException, ValueError) as exc:
        raise ImportSourceError(f"{path.name}: not a readable Safari Bookmarks.plist ({exc})") from exc
    out: list[ParsedBookmark] = []
    children = payload.get("Children", []) if isinstance(payload, dict) else []
    _walk_safari(children, [], out)
    return out


def _walk_safari(children: list, folder: list[str], out: list[ParsedBookmark]) -> None:
    for node in children:
        if not isinstance(node, dict):
            continue
        url = str(node.get("URLString") or "")
        title = str(node.get("Title") or "")
        if url:
            uri = node.get("URIDictionary")
            leaf_title = str(uri.get("title") or "") if isinstance(uri, dict) else ""
            # A leaf keeps its name in URIDictionary; only folders carry a top-level Title.
            out.append(ParsedBookmark(url=url, title=title or leaf_title, folder=join_folder(folder)))
        nested = node.get("Children")
        if isinstance(nested, list) and nested:
            name = title if title not in ("", "com.apple.ReadingList") else ("Reading List" if title else "")
            _walk_safari(nested, [*folder, name] if name else folder, out)


def folder_prefix(bookmarks: list[ParsedBookmark], prefix: str) -> None:
    """Put every imported bookmark under *prefix* (``--into "Imported/Laptop"``)."""
    base = split_folder(prefix)
    if not base:
        return
    for bm in bookmarks:
        bm.folder = join_folder([*base, *split_folder(bm.folder)])
