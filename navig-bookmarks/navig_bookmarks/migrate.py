"""Copy navig core's old ``links.db`` into ``bookmarks.db`` — one way, read-only, idempotent.

``navig links`` stored bookmarks in ``<data dir>/links.db``. This plugin replaces it, but for
one release core still WRITES that file (``navig import --persist-bookmarks``, the browser
orchestrator), so a single copy on first run would miss anything added after it. The copy is
therefore incremental and keyed by the old row id:

* every ``links`` row whose id is not yet in ``migrated_links`` is copied (same id, same
  timestamps, its tags as ``user`` tags) and its id recorded;
* a row already copied is never copied again — so a bookmark you delete here stays deleted;
* the source is opened read-only (``mode=ro``) and never written. If SQLite cannot open it
  read-only in place (a WAL file it may not touch), a private copy is read instead;
* a stat signature of ``links.db`` (+ its ``-wal``) is recorded, so an unchanged source is
  not even opened. Running it twice is a no-op.

Core's import path stored a browser folder as the note ``"Imported folder: <path>"``; such a
note becomes the bookmark's ``folder_path`` (the note itself is dropped, it said nothing else).
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from navig_sdk.host import safe_json_loads
from navig_sdk.store import utcnow as _utcnow

from navig_bookmarks.models import join_folder
from navig_bookmarks.store import BookmarkStore
from navig_bookmarks.urls import dedupe_key

LEGACY_FILENAME = "links.db"
_SIG_KEY = "legacy_links_signature"
_AT_KEY = "legacy_links_migrated_at"
_FOLDER_NOTE = re.compile(r"^Imported folder:\s*(.*)$")


@dataclass
class MigrationResult:
    source: str
    status: str  # "absent" | "unchanged" | "copied"
    copied: int = 0
    skipped_duplicates: int = 0
    already_migrated: int = 0


def legacy_path() -> Path:
    from navig_sdk.host import data_dir  # noqa: PLC0415

    return data_dir() / LEGACY_FILENAME


def _signature(source: Path) -> str:
    parts = []
    for p in (source, source.with_name(source.name + "-wal")):
        try:
            st = p.stat()
        except OSError:
            parts.append("-")
            continue
        parts.append(f"{st.st_mtime_ns}:{st.st_size}")
    return "|".join(parts)


def _read_rows(source: Path) -> list[dict]:
    try:
        con = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True, timeout=5)
        try:
            return [dict(r) for r in _select(con)]
        finally:
            con.close()
    except sqlite3.OperationalError:
        pass
    # Read-only in place was refused (typically a WAL database whose -shm we may not
    # create). Read a private copy instead; the original is still never written.
    with tempfile.TemporaryDirectory(prefix="navig-bookmarks-links-") as tmp:
        copy = Path(tmp) / source.name
        shutil.copy2(source, copy)
        for suffix in ("-wal", "-shm"):
            side = source.with_name(source.name + suffix)
            if side.exists():
                shutil.copy2(side, copy.with_name(copy.name + suffix))
        con = sqlite3.connect(str(copy), timeout=5)
        try:
            return [dict(r) for r in _select(con)]
        finally:
            con.close()


def _select(con: sqlite3.Connection) -> list[sqlite3.Row]:
    con.row_factory = sqlite3.Row
    has_table = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='links'").fetchone()
    if not has_table:
        return []
    return con.execute("SELECT * FROM links ORDER BY rowid").fetchall()


def migrate_legacy_links(store: BookmarkStore, source: Path | None = None) -> MigrationResult:
    """Copy new rows from core's ``links.db`` into *store*. Safe to call on every run."""
    src = Path(source) if source is not None else legacy_path()
    if not src.exists():
        return MigrationResult(source=str(src), status="absent")
    sig = _signature(src)
    if store.meta_get(_SIG_KEY) == sig:
        return MigrationResult(source=str(src), status="unchanged")

    rows = _read_rows(src)
    result = MigrationResult(source=str(src), status="copied")
    now = _utcnow()
    with store.transaction() as tx:
        done = {r[0] for r in tx.execute("SELECT id FROM migrated_links")}
        present_ids = {r[0] for r in tx.execute("SELECT id FROM bookmarks")}
        present_keys = {r[0] for r in tx.execute("SELECT url_normalized FROM bookmarks")}
        for row in rows:
            lid = str(row.get("id") or "").strip()
            url = str(row.get("url") or "").strip()
            if not lid or not url:
                continue
            if lid in done:
                result.already_migrated += 1
                continue
            key = dedupe_key(url)
            if lid in present_ids or key in present_keys:
                result.skipped_duplicates += 1
            else:
                notes = row.get("notes")
                folder = ""
                match = _FOLDER_NOTE.match(str(notes or ""))
                if match:
                    folder = join_folder([p for p in match.group(1).split("/") if p.strip()])
                    notes = None
                tags = safe_json_loads(row.get("tags"), [])
                if not isinstance(tags, list):
                    tags = []
                store.add(
                    url, title=row.get("title"), notes=notes, folder=folder,
                    tags=[str(t) for t in tags if str(t).strip()], tag_source="user",
                    vault_cred_id=row.get("vault_cred_id"), created_at=row.get("created_at"),
                    bookmark_id=lid, conn=tx,
                )
                tx.execute(
                    "UPDATE bookmarks SET visit_count = ?, last_visited = ?, screenshot_path = ?, favicon_path = ? WHERE id = ?",
                    (int(row.get("visit_count") or 0), row.get("last_visited"), row.get("screenshot_path"),
                     row.get("favicon_path"), lid),
                )
                present_ids.add(lid)
                present_keys.add(key)
                result.copied += 1
            tx.execute("INSERT OR IGNORE INTO migrated_links (id, migrated_at) VALUES (?, ?)", (lid, now))
        store.meta_set(_SIG_KEY, sig, conn=tx)
        store.meta_set(_AT_KEY, now, conn=tx)
    return result
