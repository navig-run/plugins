"""The bookmark store: one SQLite file (``<navig data dir>/bookmarks.db``), FTS5 search.

Built on :class:`navig_sdk.store.BaseStore`, which is navig's own storage engine inside navig
(WAL, ``busy_timeout=5000``, per-database write lock) and a faithful standalone twin without
it, so the same file opens either way.

Tables:

* ``bookmarks`` — one row per bookmark. ``url_normalized`` (indexed) is the duplicate key
  (:func:`navig_bookmarks.urls.dedupe_key`); ``folder_path`` is "/"-joined with escapes
  (:func:`navig_bookmarks.models.join_folder`); ``http_status`` / ``final_url`` /
  ``checked_at`` / ``check_error`` hold the last ``check``. ``tags_text`` is a denormalised
  copy of the tags, kept only so FTS can search them.
* ``bookmark_tags`` — ``(bookmark_id, tag, source)``; ``source`` is ``user``, ``ai`` or
  ``rule``. An automatic tagger replaces only its own source, so re-running AI tagging never
  removes a tag a person added.
* ``bookmarks_fts`` — external-content FTS5 over url/title/notes/tags/folder. Its triggers
  use the ``'delete'`` command form; a plain ``UPDATE``/``DELETE`` on an external-content
  table desyncs the index (core's links store learned that the hard way).
* ``tag_rules`` — offline ``pattern -> tags`` rules (:mod:`navig_bookmarks.rules`).
* ``meta`` and ``migrated_links`` — bookkeeping for the one-way copy from core's links store.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from navig_sdk.store import BaseStore
from navig_sdk.store import utcnow as _utcnow

from navig_bookmarks.models import TAG_SOURCES, Bookmark, ParsedBookmark
from navig_bookmarks.urls import dedupe_key

DB_FILENAME = "bookmarks.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bookmarks (
    id              TEXT PRIMARY KEY,
    url             TEXT NOT NULL,
    url_normalized  TEXT NOT NULL DEFAULT '',
    title           TEXT,
    notes           TEXT,
    folder_path     TEXT NOT NULL DEFAULT '',
    tags_text       TEXT NOT NULL DEFAULT '',
    vault_cred_id   TEXT,
    last_visited    TEXT,
    visit_count     INTEGER NOT NULL DEFAULT 0,
    screenshot_path TEXT,
    favicon_path    TEXT,
    http_status     INTEGER,
    final_url       TEXT,
    checked_at      TEXT,
    check_error     TEXT,
    created_at      TEXT,
    updated_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_bookmarks_url_normalized ON bookmarks(url_normalized);
CREATE INDEX IF NOT EXISTS idx_bookmarks_folder ON bookmarks(folder_path);

CREATE TABLE IF NOT EXISTS bookmark_tags (
    bookmark_id TEXT NOT NULL REFERENCES bookmarks(id) ON DELETE CASCADE,
    tag         TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'user' CHECK (source IN ('user', 'ai', 'rule')),
    created_at  TEXT,
    PRIMARY KEY (bookmark_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_bookmark_tags_tag ON bookmark_tags(tag);

CREATE TABLE IF NOT EXISTS tag_rules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern    TEXT NOT NULL,
    tags       TEXT NOT NULL,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS migrated_links (
    id          TEXT PRIMARY KEY,
    migrated_at TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS bookmarks_fts USING fts5(
    url, title, notes, tags_text, folder_path,
    content='bookmarks', content_rowid='rowid'
);

DROP TRIGGER IF EXISTS bookmarks_fts_insert;
DROP TRIGGER IF EXISTS bookmarks_fts_update;
DROP TRIGGER IF EXISTS bookmarks_fts_delete;

CREATE TRIGGER bookmarks_fts_insert AFTER INSERT ON bookmarks BEGIN
    INSERT INTO bookmarks_fts(rowid, url, title, notes, tags_text, folder_path)
    VALUES (new.rowid, new.url, COALESCE(new.title,''), COALESCE(new.notes,''), new.tags_text, new.folder_path);
END;
CREATE TRIGGER bookmarks_fts_delete AFTER DELETE ON bookmarks BEGIN
    INSERT INTO bookmarks_fts(bookmarks_fts, rowid, url, title, notes, tags_text, folder_path)
    VALUES ('delete', old.rowid, old.url, COALESCE(old.title,''), COALESCE(old.notes,''), old.tags_text, old.folder_path);
END;
CREATE TRIGGER bookmarks_fts_update AFTER UPDATE ON bookmarks BEGIN
    INSERT INTO bookmarks_fts(bookmarks_fts, rowid, url, title, notes, tags_text, folder_path)
    VALUES ('delete', old.rowid, old.url, COALESCE(old.title,''), COALESCE(old.notes,''), old.tags_text, old.folder_path);
    INSERT INTO bookmarks_fts(rowid, url, title, notes, tags_text, folder_path)
    VALUES (new.rowid, new.url, COALESCE(new.title,''), COALESCE(new.notes,''), new.tags_text, new.folder_path);
END;
"""

_SYNC_TAGS_TEXT = """UPDATE bookmarks SET tags_text = COALESCE(
    (SELECT group_concat(tag, ' ') FROM (SELECT tag FROM bookmark_tags WHERE bookmark_id = ? ORDER BY tag)), '')
    WHERE id = ?"""


def default_db_path() -> Path:
    """``$NAVIG_BOOKMARKS_DB`` when set, else ``<navig data dir>/bookmarks.db``."""
    override = os.environ.get("NAVIG_BOOKMARKS_DB", "").strip()
    if override:
        return Path(override)
    from navig_sdk.host import data_dir  # noqa: PLC0415 - resolved per call, never at import

    return data_dir() / DB_FILENAME


def new_id() -> str:
    return uuid.uuid4().hex[:8]


def clean_tag(tag: str) -> str:
    """Tags are lowercase, single tokens: ``"Machine Learning"`` -> ``machine-learning``."""
    t = "-".join(str(tag).strip().lower().replace(",", " ").split())
    return t.strip("-#")[:48]


class BookmarkStore(BaseStore):
    SCHEMA_VERSION = 1
    PRAGMAS = {"foreign_keys": "ON"}

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(_SCHEMA)

    # ── transactions ──────────────────────────────────────────────────────────

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One ``BEGIN IMMEDIATE`` … ``COMMIT`` for a multi-statement change; rolls back on error."""
        with self._lock:
            conn = self._get_conn()
            old = conn.isolation_level
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.isolation_level = old

    # ── rows ──────────────────────────────────────────────────────────────────

    def _hydrate(self, rows: list[sqlite3.Row]) -> list[Bookmark]:
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        tags: dict[str, dict[str, str]] = {i: {} for i in ids}
        for chunk_start in range(0, len(ids), 500):
            chunk = ids[chunk_start : chunk_start + 500]
            marks = ",".join("?" * len(chunk))
            for t in self._read_all(
                f"SELECT bookmark_id, tag, source FROM bookmark_tags WHERE bookmark_id IN ({marks})", tuple(chunk)
            ):
                tags[t["bookmark_id"]][t["tag"]] = t["source"]
        out = []
        for r in rows:
            out.append(
                Bookmark(
                    id=r["id"], url=r["url"], url_normalized=r["url_normalized"], title=r["title"],
                    notes=r["notes"], folder_path=r["folder_path"] or "", tags=tags[r["id"]],
                    vault_cred_id=r["vault_cred_id"], visit_count=int(r["visit_count"] or 0),
                    last_visited=r["last_visited"], http_status=r["http_status"], final_url=r["final_url"],
                    checked_at=r["checked_at"], check_error=r["check_error"], created_at=r["created_at"],
                    updated_at=r["updated_at"],
                )
            )
        return out

    def get(self, bookmark_id: str) -> Bookmark | None:
        rows = self._read_all("SELECT * FROM bookmarks WHERE id = ?", (bookmark_id,))
        hydrated = self._hydrate(rows)
        return hydrated[0] if hydrated else None

    def resolve_id(self, ref: str) -> Bookmark | None:
        """An exact id, or a unique id prefix (``3f2a`` for ``3f2a9c01``)."""
        hit = self.get(ref)
        if hit or len(ref) < 3:
            return hit
        rows = self._read_all("SELECT * FROM bookmarks WHERE id LIKE ? LIMIT 2", (ref.replace("%", "") + "%",))
        return self._hydrate(rows)[0] if len(rows) == 1 else None

    def find_duplicate(self, url: str) -> Bookmark | None:
        rows = self._read_all("SELECT * FROM bookmarks WHERE url_normalized = ? LIMIT 1", (dedupe_key(url),))
        hydrated = self._hydrate(rows)
        return hydrated[0] if hydrated else None

    def count(self) -> int:
        row = self._read_one("SELECT COUNT(*) FROM bookmarks")
        return int(row[0]) if row else 0

    def list_bookmarks(
        self,
        *,
        tag: str | None = None,
        folder: str | None = None,
        cred: str | None = None,
        untagged: bool = False,
        limit: int | None = None,
        offset: int = 0,
        order: str = "recent",
    ) -> list[Bookmark]:
        where, params = [], []
        if tag:
            where.append("id IN (SELECT bookmark_id FROM bookmark_tags WHERE tag = ?)")
            params.append(clean_tag(tag))
        if folder is not None:
            where.append("(folder_path = ? OR folder_path LIKE ?)")
            params += [folder, folder.replace("%", "") + "/%"]
        if cred:
            where.append("vault_cred_id = ?")
            params.append(cred)
        if untagged:
            where.append("NOT EXISTS (SELECT 1 FROM bookmark_tags t WHERE t.bookmark_id = bookmarks.id)")
        sql = "SELECT * FROM bookmarks"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += (
            " ORDER BY rowid" if order == "insertion"
            else " ORDER BY COALESCE(last_visited, created_at) DESC, rowid DESC"
        )
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params += [limit, offset]
        return self._hydrate(self._read_all(sql, tuple(params)))

    def search(self, query: str, limit: int = 20) -> list[Bookmark]:
        """FTS5 search; falls back to quoted tokens, then to LIKE, so no query ever errors."""
        q = query.strip()
        if not q:
            return self.list_bookmarks(limit=limit)
        sql = (
            "SELECT b.* FROM bookmarks b JOIN bookmarks_fts f ON b.rowid = f.rowid "
            "WHERE bookmarks_fts MATCH ? ORDER BY rank LIMIT ?"
        )
        quoted = " ".join('"' + tok.replace('"', '""') + '"*' for tok in q.split())
        for candidate in (q, quoted):
            try:
                return self._hydrate(self._read_all(sql, (candidate, limit)))
            except sqlite3.DatabaseError:
                continue
        like = f"%{q}%"
        return self._hydrate(
            self._read_all(
                "SELECT * FROM bookmarks WHERE url LIKE ? OR title LIKE ? OR notes LIKE ? OR tags_text LIKE ? LIMIT ?",
                (like, like, like, like, limit),
            )
        )

    def folders(self) -> dict[str, int]:
        return {r[0]: int(r[1]) for r in self._read_all(
            "SELECT folder_path, COUNT(*) FROM bookmarks GROUP BY folder_path ORDER BY folder_path"
        )}

    def tag_counts(self) -> dict[str, int]:
        return {r[0]: int(r[1]) for r in self._read_all(
            "SELECT tag, COUNT(*) FROM bookmark_tags GROUP BY tag ORDER BY COUNT(*) DESC, tag"
        )}

    # ── writes ────────────────────────────────────────────────────────────────

    def add(
        self,
        url: str,
        *,
        title: str | None = None,
        notes: str | None = None,
        folder: str = "",
        tags: Iterable[str] = (),
        tag_source: str = "user",
        vault_cred_id: str | None = None,
        created_at: str | None = None,
        bookmark_id: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> str:
        """Insert one bookmark and return its id. Pass *conn* to join an open transaction."""
        if conn is None:
            with self.transaction() as tx:
                return self.add(url, title=title, notes=notes, folder=folder, tags=tags, tag_source=tag_source,
                                vault_cred_id=vault_cred_id, created_at=created_at, bookmark_id=bookmark_id, conn=tx)
        bid = bookmark_id or new_id()
        now = _utcnow()
        conn.execute(
            """INSERT INTO bookmarks (id, url, url_normalized, title, notes, folder_path, vault_cred_id,
                                      created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)""",
            (bid, url, dedupe_key(url), title or None, notes or None, folder or "", vault_cred_id,
             created_at or now, now),
        )
        self._add_tags(conn, bid, tags, tag_source)
        return bid

    def _add_tags(self, conn: sqlite3.Connection, bid: str, tags: Iterable[str], source: str) -> int:
        if source not in TAG_SOURCES:
            raise ValueError(f"tag source must be one of {TAG_SOURCES}")
        now = _utcnow()
        added = 0
        for raw in tags:
            tag = clean_tag(raw)
            if not tag:
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO bookmark_tags (bookmark_id, tag, source, created_at) VALUES (?,?,?,?)",
                (bid, tag, source, now),
            )
            if source == "user" and not cur.rowcount:
                # A person claiming a tag an automatic tagger set makes it theirs.
                conn.execute("UPDATE bookmark_tags SET source = 'user' WHERE bookmark_id = ? AND tag = ?", (bid, tag))
            added += cur.rowcount
        conn.execute(_SYNC_TAGS_TEXT, (bid, bid))
        return added

    def add_tags(self, bookmark_id: str, tags: Iterable[str], source: str = "user") -> int:
        with self.transaction() as tx:
            return self._add_tags(tx, bookmark_id, tags, source)

    def remove_tags(self, bookmark_id: str, tags: Iterable[str]) -> int:
        names = [clean_tag(t) for t in tags if clean_tag(t)]
        with self.transaction() as tx:
            removed = 0
            for tag in names:
                removed += tx.execute(
                    "DELETE FROM bookmark_tags WHERE bookmark_id = ? AND tag = ?", (bookmark_id, tag)
                ).rowcount
            tx.execute(_SYNC_TAGS_TEXT, (bookmark_id, bookmark_id))
            return removed

    def replace_source_tags(
        self, bookmark_id: str, tags: Iterable[str], source: str, conn: sqlite3.Connection | None = None
    ) -> tuple[list[str], list[str]]:
        """Make *source*'s tags on a bookmark exactly *tags*; other sources are untouched.

        A tag a person (or a higher-priority source) already holds is not duplicated or
        downgraded. Returns ``(added, removed)``.
        """
        if source not in ("ai", "rule"):
            raise ValueError("only automatic sources (ai, rule) are replaced wholesale")
        if conn is None:
            with self.transaction() as tx:
                return self.replace_source_tags(bookmark_id, tags, source, conn=tx)
        wanted = {clean_tag(t) for t in tags if clean_tag(t)}
        rows = conn.execute("SELECT tag, source FROM bookmark_tags WHERE bookmark_id = ?", (bookmark_id,)).fetchall()
        held = {r[0]: r[1] for r in rows}
        mine = {t for t, s in held.items() if s == source}
        removed = sorted(mine - wanted)
        for tag in removed:
            conn.execute("DELETE FROM bookmark_tags WHERE bookmark_id = ? AND tag = ? AND source = ?",
                         (bookmark_id, tag, source))
        added = sorted(t for t in wanted if t not in held)
        self._add_tags(conn, bookmark_id, added, source)
        conn.execute(_SYNC_TAGS_TEXT, (bookmark_id, bookmark_id))
        return added, removed

    def update(
        self,
        bookmark_id: str,
        *,
        url: str | None = None,
        title: str | None = None,
        notes: str | None = None,
        folder: str | None = None,
        vault_cred_id: str | None = None,
    ) -> bool:
        sets, params = [], []
        if url is not None:
            sets += ["url = ?", "url_normalized = ?"]
            params += [url, dedupe_key(url)]
        if title is not None:
            sets.append("title = ?")
            params.append(title or None)
        if notes is not None:
            sets.append("notes = ?")
            params.append(notes or None)
        if folder is not None:
            sets.append("folder_path = ?")
            params.append(folder)
        if vault_cred_id is not None:
            sets.append("vault_cred_id = ?")
            params.append(vault_cred_id or None)
        if not sets:
            return self.get(bookmark_id) is not None
        sets.append("updated_at = ?")
        params += [_utcnow(), bookmark_id]
        return self._write(f"UPDATE bookmarks SET {', '.join(sets)} WHERE id = ?", tuple(params)).rowcount > 0

    def delete(self, bookmark_id: str) -> bool:
        with self.transaction() as tx:
            tx.execute("DELETE FROM bookmark_tags WHERE bookmark_id = ?", (bookmark_id,))
            return tx.execute("DELETE FROM bookmarks WHERE id = ?", (bookmark_id,)).rowcount > 0

    def record_visit(self, bookmark_id: str) -> None:
        self._write(
            "UPDATE bookmarks SET visit_count = visit_count + 1, last_visited = ? WHERE id = ?",
            (_utcnow(), bookmark_id),
        )

    def set_check_result(self, rows: Iterable[tuple[str, int | None, str | None, str | None]]) -> int:
        """``(id, http_status, final_url, error)`` per bookmark, stamped with one ``checked_at``."""
        now = _utcnow()
        payload = [(status, final, err, now, bid) for bid, status, final, err in rows]
        return self._write_many(
            "UPDATE bookmarks SET http_status = ?, final_url = ?, check_error = ?, checked_at = ? WHERE id = ?",
            payload,
        )

    def apply_folders(self, plan: dict[str, str]) -> int:
        """Move every bookmark in *plan* (``id -> folder``) in ONE transaction."""
        now = _utcnow()
        with self.transaction() as tx:
            moved = 0
            for bid, folder in plan.items():
                moved += tx.execute(
                    "UPDATE bookmarks SET folder_path = ?, updated_at = ? WHERE id = ? AND folder_path <> ?",
                    (folder, now, bid, folder),
                ).rowcount
            return moved

    def import_many(
        self, items: Iterable[ParsedBookmark], *, tag_source: str = "user", rule_tagger=None,
        vault_cred_id: str | None = None,
    ) -> tuple[int, int]:
        """Add every item whose URL is not already stored. Returns ``(added, skipped)``."""
        added = skipped = 0
        seen: set[str] = set()
        with self.transaction() as tx:
            existing = {r[0] for r in tx.execute("SELECT url_normalized FROM bookmarks")}
            for item in items:
                key = dedupe_key(item.url)
                if not item.url or key in existing or key in seen:
                    skipped += 1
                    continue
                seen.add(key)
                created = None
                if item.added:
                    from datetime import datetime, timezone  # noqa: PLC0415

                    try:
                        created = datetime.fromtimestamp(item.added, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                    except (OverflowError, OSError, ValueError):
                        created = None
                bid = self.add(item.url, title=item.title, notes=item.notes, folder=item.folder, tags=item.tags,
                               tag_source=tag_source, vault_cred_id=vault_cred_id, created_at=created, conn=tx)
                if rule_tagger is not None:
                    rule_tags = rule_tagger(item.url)
                    if rule_tags:
                        self._add_tags(tx, bid, rule_tags, "rule")
                added += 1
        return added, skipped

    def merge(self, keep_id: str, drop_ids: list[str]) -> int:
        """Fold *drop_ids* into *keep_id*: tags (a ``user`` tag wins), notes, visits, credential."""
        with self.transaction() as tx:
            keep = tx.execute("SELECT * FROM bookmarks WHERE id = ?", (keep_id,)).fetchone()
            if keep is None:
                return 0
            notes = [keep["notes"]] if keep["notes"] else []
            visits = int(keep["visit_count"] or 0)
            cred = keep["vault_cred_id"]
            title = keep["title"]
            merged = 0
            for did in drop_ids:
                row = tx.execute("SELECT * FROM bookmarks WHERE id = ?", (did,)).fetchone()
                if row is None or did == keep_id:
                    continue
                if row["notes"] and row["notes"] not in notes:
                    notes.append(row["notes"])
                visits += int(row["visit_count"] or 0)
                cred = cred or row["vault_cred_id"]
                title = title or row["title"]
                for tag, source in tx.execute(
                    "SELECT tag, source FROM bookmark_tags WHERE bookmark_id = ?", (did,)
                ).fetchall():
                    self._add_tags(tx, keep_id, [tag], source)
                tx.execute("DELETE FROM bookmark_tags WHERE bookmark_id = ?", (did,))
                tx.execute("DELETE FROM bookmarks WHERE id = ?", (did,))
                merged += 1
            tx.execute(
                "UPDATE bookmarks SET notes = ?, visit_count = ?, vault_cred_id = ?, title = ?, updated_at = ? WHERE id = ?",
                ("\n".join(notes) or None, visits, cred, title, _utcnow(), keep_id),
            )
            return merged

    # ── rules ─────────────────────────────────────────────────────────────────

    def rules(self) -> list[tuple[int, str, list[str]]]:
        return [(int(r[0]), r[1], r[2].split()) for r in self._read_all(
            "SELECT id, pattern, tags FROM tag_rules ORDER BY id"
        )]

    def add_rule(self, pattern: str, tags: list[str]) -> int:
        cleaned = " ".join(clean_tag(t) for t in tags if clean_tag(t))
        cur = self._write(
            "INSERT INTO tag_rules (pattern, tags, created_at) VALUES (?,?,?)", (pattern.strip(), cleaned, _utcnow())
        )
        return int(cur.lastrowid or 0)

    def remove_rule(self, rule_id: int) -> bool:
        return self._write("DELETE FROM tag_rules WHERE id = ?", (rule_id,)).rowcount > 0

    # ── meta ──────────────────────────────────────────────────────────────────

    def meta_get(self, key: str) -> str | None:
        row = self._read_one("SELECT value FROM meta WHERE key = ?", (key,))
        return row[0] if row else None

    def meta_set(self, key: str, value: str, conn: sqlite3.Connection | None = None) -> None:
        sql, params = "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
        if conn is not None:
            conn.execute(sql, params)
        else:
            self._write(sql, params)
