"""The chat library: one SQLite file (``<navig data dir>/chats.db``), FTS5 search.

Its own database on purpose — not core's ``memory.db``. Imported chats are the person's
archive, not the agent's working memory; mixing them would silently widen what the agent
recalls. Exposing a folder to NAVIG's knowledge index is a separate, explicit opt-in.

Tables:

* ``conversations`` — one row per chat, keyed ``<provider>:<native id>``. The person's own
  organisation lives here too (``alias``, ``pinned``, ``favorite``, ``archived``,
  ``sort_key``) and a re-import or a live capture never touches those columns.
* ``messages`` — every message of every branch the library has seen, keyed
  ``(conversation_id, mid)`` where ``mid`` is the provider's message id. ``position`` orders
  them; ``on_active_branch`` marks the branch the provider showed last.
* ``folders`` / ``conversation_folders`` — NAVIG's virtual folders, a local overlay. A chat
  can sit in several folders; its messages are never copied. These are NOT ChatGPT Projects
  (``remote_project_id`` records those, read-only).
* ``conversation_tags``, ``bookmarks`` (a message marked as a bookmark, decision, snippet or
  todo, with a note), ``imports`` (resumable import jobs), ``meta``.
* ``messages_fts`` / ``conversations_fts`` — external-content FTS5 kept in sync by triggers
  that use the ``'delete'`` command form (a plain UPDATE/DELETE on an external-content table
  desyncs the index). Both content tables have an explicit ``INTEGER PRIMARY KEY`` so a
  ``VACUUM`` can never renumber the rowids the index points at.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from navig_sdk.store import BaseStore
from navig_sdk.store import utcnow as _utcnow

from navig_chats import query as fts_query
from navig_chats.models import (
    BOOKMARK_KINDS,
    COMPLETENESS,
    ConversationIn,
    merge_completeness,
)

DB_FILENAME = "chats.db"

#: Snippet highlight markers: control characters a page can never contain, so a surface
#: HTML-escapes the snippet first and then turns these into <mark> safely.
MARK_START = "\x02"
MARK_END = "\x03"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    rid                 INTEGER PRIMARY KEY,
    id                  TEXT NOT NULL UNIQUE,
    provider            TEXT NOT NULL,
    native_id           TEXT NOT NULL,
    account_scope       TEXT NOT NULL DEFAULT '',
    url                 TEXT NOT NULL DEFAULT '',
    title               TEXT NOT NULL DEFAULT '',
    alias               TEXT,
    remote_project_id   TEXT,
    remote_project_name TEXT,
    gizmo_id            TEXT,
    created_at          TEXT,
    updated_at          TEXT,
    last_visited_at     TEXT,
    captured_at         TEXT,
    completeness        TEXT NOT NULL DEFAULT 'url_only',
    provenance          TEXT NOT NULL DEFAULT 'manual',
    content_hash        TEXT NOT NULL DEFAULT '',
    revision            INTEGER NOT NULL DEFAULT 1,
    message_count       INTEGER NOT NULL DEFAULT 0,
    archived            INTEGER NOT NULL DEFAULT 0,
    archived_remote     INTEGER NOT NULL DEFAULT 0,
    pinned              INTEGER NOT NULL DEFAULT 0,
    favorite            INTEGER NOT NULL DEFAULT 0,
    sort_key            REAL,
    added_at            TEXT,
    brain               TEXT
);
CREATE INDEX IF NOT EXISTS idx_conv_updated ON conversations(updated_at);
CREATE INDEX IF NOT EXISTS idx_conv_created ON conversations(created_at);
CREATE INDEX IF NOT EXISTS idx_conv_visited ON conversations(last_visited_at);
CREATE INDEX IF NOT EXISTS idx_conv_project ON conversations(remote_project_id);

CREATE TABLE IF NOT EXISTS messages (
    rid              INTEGER PRIMARY KEY,
    conversation_id  TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    mid              TEXT NOT NULL,
    parent_id        TEXT,
    position         REAL NOT NULL DEFAULT 0,
    on_active_branch INTEGER NOT NULL DEFAULT 1,
    role             TEXT NOT NULL,
    content_type     TEXT NOT NULL DEFAULT 'text',
    text             TEXT NOT NULL DEFAULT '',
    parts_json       TEXT,
    attachments_json TEXT,
    has_code         INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT,
    model            TEXT,
    provenance       TEXT NOT NULL DEFAULT 'manual',
    hash             TEXT NOT NULL DEFAULT '',
    UNIQUE (conversation_id, mid)
);
CREATE INDEX IF NOT EXISTS idx_msg_conv_pos ON messages(conversation_id, position);

CREATE TABLE IF NOT EXISTS folders (
    id         TEXT PRIMARY KEY,
    parent_id  TEXT REFERENCES folders(id) ON DELETE SET NULL,
    name       TEXT NOT NULL,
    color      TEXT,
    icon       TEXT,
    sort_key   REAL NOT NULL DEFAULT 0,
    pinned     INTEGER NOT NULL DEFAULT 0,
    created_at TEXT,
    updated_at TEXT,
    brain         TEXT,
    brain_project TEXT
);

CREATE TABLE IF NOT EXISTS conversation_folders (
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    folder_id       TEXT NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    added_at        TEXT,
    PRIMARY KEY (conversation_id, folder_id)
);
CREATE INDEX IF NOT EXISTS idx_cf_folder ON conversation_folders(folder_id);

CREATE TABLE IF NOT EXISTS conversation_tags (
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    tag             TEXT NOT NULL,
    PRIMARY KEY (conversation_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_ct_tag ON conversation_tags(tag);

CREATE TABLE IF NOT EXISTS bookmarks (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    message_id      TEXT,
    kind            TEXT NOT NULL DEFAULT 'bookmark',
    note            TEXT,
    quote           TEXT,
    created_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_bm_conv ON bookmarks(conversation_id);

CREATE TABLE IF NOT EXISTS imports (
    id          TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    status      TEXT NOT NULL,
    total       INTEGER NOT NULL DEFAULT 0,
    done        INTEGER NOT NULL DEFAULT 0,
    added       INTEGER NOT NULL DEFAULT 0,
    updated     INTEGER NOT NULL DEFAULT 0,
    unchanged   INTEGER NOT NULL DEFAULT 0,
    errors_json TEXT NOT NULL DEFAULT '[]',
    started_at  TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text, content='messages', content_rowid='rid',
    tokenize='porter unicode61 remove_diacritics 2'
);
CREATE VIRTUAL TABLE IF NOT EXISTS conversations_fts USING fts5(
    title, alias, content='conversations', content_rowid='rid',
    tokenize='porter unicode61 remove_diacritics 2'
);

DROP TRIGGER IF EXISTS messages_fts_insert;
DROP TRIGGER IF EXISTS messages_fts_update;
DROP TRIGGER IF EXISTS messages_fts_delete;
DROP TRIGGER IF EXISTS conversations_fts_insert;
DROP TRIGGER IF EXISTS conversations_fts_update;
DROP TRIGGER IF EXISTS conversations_fts_delete;

CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text) VALUES (new.rid, new.text);
END;
CREATE TRIGGER messages_fts_delete AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.rid, old.text);
END;
CREATE TRIGGER messages_fts_update AFTER UPDATE OF text ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.rid, old.text);
    INSERT INTO messages_fts(rowid, text) VALUES (new.rid, new.text);
END;

CREATE TRIGGER conversations_fts_insert AFTER INSERT ON conversations BEGIN
    INSERT INTO conversations_fts(rowid, title, alias) VALUES (new.rid, new.title, COALESCE(new.alias, ''));
END;
CREATE TRIGGER conversations_fts_delete AFTER DELETE ON conversations BEGIN
    INSERT INTO conversations_fts(conversations_fts, rowid, title, alias)
    VALUES ('delete', old.rid, old.title, COALESCE(old.alias, ''));
END;
CREATE TRIGGER conversations_fts_update AFTER UPDATE OF title, alias ON conversations BEGIN
    INSERT INTO conversations_fts(conversations_fts, rowid, title, alias)
    VALUES ('delete', old.rid, old.title, COALESCE(old.alias, ''));
    INSERT INTO conversations_fts(rowid, title, alias) VALUES (new.rid, new.title, COALESCE(new.alias, ''));
END;
"""

SORTS = {
    "newest": "COALESCE(c.updated_at, c.created_at, c.added_at) DESC",
    "oldest": "COALESCE(c.created_at, c.updated_at, c.added_at) ASC",
    "created": "COALESCE(c.created_at, c.added_at) DESC",
    "alpha": "COALESCE(NULLIF(c.alias, ''), c.title) COLLATE NOCASE ASC",
    "visited": "c.last_visited_at IS NULL, c.last_visited_at DESC",
    "manual": "c.sort_key IS NULL, c.sort_key ASC, COALESCE(c.updated_at, c.added_at) DESC",
}

COLLECTIONS = ("all", "recent", "uncategorized", "partial", "pinned", "favorites", "archived", "projects")

#: Titles a page shows before the real one is known — never overwrite a real title with one.
GENERIC_TITLES = frozenset({"", "chatgpt", "new chat", "untitled"})


class NotFound(LookupError):
    pass


class _OverBudget(Exception):
    pass


def default_db_path() -> Path:
    """``$NAVIG_CHATS_DB`` when set, else ``<navig data dir>/chats.db``."""
    override = os.environ.get("NAVIG_CHATS_DB", "").strip()
    if override:
        return Path(override)
    from navig_sdk.host import data_dir  # noqa: PLC0415 - resolved per call, never at import

    return data_dir() / DB_FILENAME


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def clean_tag(tag: str) -> str:
    t = "-".join(str(tag).strip().lower().replace(",", " ").split())
    return t.strip("-#")[:48]


def _b(v: Any) -> int:
    return 1 if v else 0


#: How far a chat may travel beyond the library. ``off``: nowhere. ``local``: NAVIG's own
#: knowledge index on this computer (``navig recall``), never sent anywhere. ``agent``: also
#: allowed into agent context, which is sent to the AI provider the person configured.
BRAIN_LEVELS = ("off", "local", "agent")
_BRAIN_RANK = {lvl: i for i, lvl in enumerate(BRAIN_LEVELS)}


class ChatStore(BaseStore):
    SCHEMA_VERSION = 2
    PRAGMAS = {"foreign_keys": "ON"}
    #: Seconds a relevance-ranked search may take before it falls back to newest matches.
    SEARCH_BUDGET_S = 0.35

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(_SCHEMA)

    def _migrate(self, conn: sqlite3.Connection, from_version: int, to_version: int) -> None:
        if from_version < 2:
            # v2: per-chat and per-folder sharing levels (additive; NULL = inherit / off).
            cols = {r[1] for r in conn.execute("PRAGMA table_info(conversations)")}
            if "brain" not in cols:
                conn.execute("ALTER TABLE conversations ADD COLUMN brain TEXT")
            fcols = {r[1] for r in conn.execute("PRAGMA table_info(folders)")}
            if "brain" not in fcols:
                conn.execute("ALTER TABLE folders ADD COLUMN brain TEXT")
            if "brain_project" not in fcols:
                conn.execute("ALTER TABLE folders ADD COLUMN brain_project TEXT")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One ``BEGIN IMMEDIATE`` … ``COMMIT``; rolls back on error."""
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

    # ── conversations in ──────────────────────────────────────────────────────

    def upsert(self, conv: ConversationIn, *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        """Add or merge one conversation. Returns ``{"id", "status", "revision"}``.

        ``status`` is ``added``, ``updated`` or ``unchanged``. Never touched by a merge: the
        alias, pins, favourite, archive flag, manual order, folders, tags and bookmarks. The
        completeness only rises. Messages merge by id; none are deleted (an older export must
        not erase what a later live capture saw).
        """
        if conn is None:
            with self.transaction() as c:
                return self.upsert(conv, conn=c)
        now = _utcnow()
        cid = conv.id
        incoming_hash = conv.content_hash()
        row = conn.execute("SELECT * FROM conversations WHERE id = ?", (cid,)).fetchone()
        title = (conv.title or "").strip()
        if row is None:
            conn.execute(
                "INSERT INTO conversations (id, provider, native_id, account_scope, url, title, remote_project_id,"
                " remote_project_name, gizmo_id, created_at, updated_at, captured_at, completeness, provenance,"
                " content_hash, revision, archived_remote, added_at, last_visited_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)",
                (
                    cid, conv.provider, conv.native_id, conv.account_scope, conv.url, title,
                    conv.remote_project_id, conv.remote_project_name, conv.gizmo_id,
                    conv.created_at,
                    conv.updated_at or conv.created_at or (now if conv.provenance == "live" else None),
                    now, conv.completeness,
                    conv.provenance, incoming_hash, _b(conv.archived_remote), now,
                    now if conv.provenance == "live" else None,
                ),
            )
            self._merge_messages(conn, conv)
            self._refresh_count(conn, cid)
            return {"id": cid, "status": "added", "revision": 1}

        completeness = merge_completeness(row["completeness"], conv.completeness)
        keep_title = row["title"] if title.lower() in GENERIC_TITLES else title
        changed_messages = self._merge_messages(conn, conv)
        meta_changed = (
            keep_title != row["title"]
            or completeness != row["completeness"]
            or (conv.url and conv.url != row["url"])
            or (conv.remote_project_id and conv.remote_project_id != row["remote_project_id"])
        )
        if not changed_messages and not meta_changed:
            if conv.provenance == "live":
                conn.execute("UPDATE conversations SET last_visited_at = ? WHERE id = ?", (now, cid))
            return {"id": cid, "status": "unchanged", "revision": row["revision"]}
        revision = row["revision"] + 1
        # A live capture carries no provider timestamp: "updated" is when NAVIG saw new content.
        seen_change = now if (conv.provenance == "live" and changed_messages) else None
        updated_at = max(filter(None, [row["updated_at"], conv.updated_at, seen_change]), default=None)
        provenance = "export" if "export" in (row["provenance"], conv.provenance) else conv.provenance
        conn.execute(
            "UPDATE conversations SET title = ?, url = COALESCE(NULLIF(?, ''), url), completeness = ?,"
            " provenance = ?, content_hash = ?, revision = ?, updated_at = ?, captured_at = ?,"
            " created_at = COALESCE(created_at, ?),"
            " remote_project_id = COALESCE(?, remote_project_id),"
            " remote_project_name = COALESCE(?, remote_project_name), gizmo_id = COALESCE(?, gizmo_id),"
            " archived_remote = ?, last_visited_at = CASE WHEN ? = 'live' THEN ? ELSE last_visited_at END"
            " WHERE id = ?",
            (
                keep_title, conv.url, completeness, provenance, incoming_hash, revision, updated_at, now,
                conv.created_at, conv.remote_project_id, conv.remote_project_name, conv.gizmo_id,
                _b(conv.archived_remote), conv.provenance, now, cid,
            ),
        )
        self._refresh_count(conn, cid)
        return {"id": cid, "status": "updated", "revision": revision}

    def _merge_messages(self, conn: sqlite3.Connection, conv: ConversationIn) -> int:
        """Insert new messages, update changed ones; returns how many rows changed."""
        cid = conv.id
        existing = {
            r["mid"]: r
            for r in conn.execute(
                "SELECT mid, position, hash, on_active_branch FROM messages WHERE conversation_id = ?", (cid,)
            )
        }
        positions = sorted(r["position"] for r in existing.values())
        changed = 0
        cursor_pos: float | None = None
        # An export carries its own tree order; a live capture is the visible order, which is
        # placed around the messages the library already holds.
        for idx, m in enumerate(conv.messages):
            digest = m.digest()
            parts_json = json.dumps(m.parts, ensure_ascii=False) if m.parts is not None else None
            attachments_json = json.dumps(m.attachments, ensure_ascii=False) if m.attachments else None
            old = existing.get(m.id)
            if old is not None:
                cursor_pos = old["position"]
                if old["hash"] != digest or (m.on_active_branch and not old["on_active_branch"]):
                    conn.execute(
                        "UPDATE messages SET role = ?, text = ?, content_type = ?, parts_json = COALESCE(?, parts_json),"
                        " attachments_json = COALESCE(?, attachments_json), has_code = ?, hash = ?,"
                        " on_active_branch = MAX(on_active_branch, ?), model = COALESCE(?, model),"
                        " created_at = COALESCE(created_at, ?), parent_id = COALESCE(parent_id, ?)"
                        " WHERE conversation_id = ? AND mid = ?",
                        (
                            m.role, m.text, m.content_type, parts_json, attachments_json, _b(m.has_code), digest,
                            _b(m.on_active_branch), m.model, m.created_at, m.parent_id, cid, m.id,
                        ),
                    )
                    changed += 1
                continue
            if not existing:
                pos = float(idx)
            elif cursor_pos is None:
                first = positions[0] if positions else 0.0
                pos = first - (len(conv.messages) - idx)
            else:
                nxt = next((p for p in positions if p > cursor_pos), None)
                pos = (cursor_pos + nxt) / 2 if nxt is not None else cursor_pos + 1.0
                positions.append(pos)
                positions.sort()
            cursor_pos = pos
            conn.execute(
                "INSERT INTO messages (conversation_id, mid, parent_id, position, on_active_branch, role,"
                " content_type, text, parts_json, attachments_json, has_code, created_at, model, provenance, hash)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    cid, m.id, m.parent_id, pos, _b(m.on_active_branch), m.role, m.content_type, m.text,
                    parts_json, attachments_json, _b(m.has_code), m.created_at, m.model, conv.provenance, digest,
                ),
            )
            changed += 1
        if conv.provenance == "live" and conv.completeness == "rendered" and conv.messages:
            # The page showed this branch whole: it IS the active branch now.
            seen = [m.id for m in conv.messages]
            in_seen = "CASE WHEN mid IN (" + ",".join("?" * len(seen)) + ") THEN 1 ELSE 0 END"
            cur = conn.execute(
                f"UPDATE messages SET on_active_branch = {in_seen}"
                f" WHERE conversation_id = ? AND on_active_branch != {in_seen}",
                (*seen, cid, *seen),
            )
            changed += max(cur.rowcount, 0)
        return changed

    def _refresh_count(self, conn: sqlite3.Connection, cid: str) -> None:
        conn.execute(
            "UPDATE conversations SET message_count ="
            " (SELECT COUNT(*) FROM messages WHERE conversation_id = ? AND on_active_branch = 1) WHERE id = ?",
            (cid, cid),
        )

    def record_visit(self, cid: str) -> dict[str, Any] | None:
        """Stamp ``last_visited_at`` on a SAVED chat; an unsaved one is not stored (returns None)."""
        with self.transaction() as conn:
            cur = conn.execute("UPDATE conversations SET last_visited_at = ? WHERE id = ?", (_utcnow(), cid))
            if cur.rowcount == 0:
                return None
        return self.get(cid)

    # ── conversations out ─────────────────────────────────────────────────────

    def _folders_of(self, ids: list[str]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {i: [] for i in ids}
        for chunk in _chunks(ids):
            marks = ",".join("?" * len(chunk))
            for r in self._read_all(
                f"SELECT conversation_id, folder_id FROM conversation_folders WHERE conversation_id IN ({marks})",
                tuple(chunk),
            ):
                out[r["conversation_id"]].append(r["folder_id"])
        return out

    def _tags_of(self, ids: list[str]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {i: [] for i in ids}
        for chunk in _chunks(ids):
            marks = ",".join("?" * len(chunk))
            for r in self._read_all(
                f"SELECT conversation_id, tag FROM conversation_tags WHERE conversation_id IN ({marks}) ORDER BY tag",
                tuple(chunk),
            ):
                out[r["conversation_id"]].append(r["tag"])
        return out

    def _summaries(self, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        ids = [r["id"] for r in rows]
        folders = self._folders_of(ids)
        tags = self._tags_of(ids)
        return [_summary(r, folders[r["id"]], tags[r["id"]]) for r in rows]

    def get(self, cid: str) -> dict[str, Any] | None:
        row = self._read_one("SELECT * FROM conversations c WHERE id = ?", (cid,))
        if row is None:
            return None
        out = self._summaries([row])[0]
        out["bookmarks"] = self.bookmarks(cid)
        out["branch_count"] = int(
            (self._read_one(
                "SELECT COUNT(*) AS n FROM messages WHERE conversation_id = ? AND on_active_branch = 0", (cid,)
            ) or {"n": 0})["n"]
        )
        return out

    def resolve(self, ref: str) -> dict[str, Any] | None:
        """A conversation by full id, by native id, or by a unique id prefix."""
        found = self.get(ref)
        if found:
            return found
        rows = self._read_all(
            "SELECT id FROM conversations WHERE native_id = ? OR id LIKE ? LIMIT 2", (ref, f"%{ref}%")
        )
        if len(rows) == 1:
            return self.get(rows[0]["id"])
        return None

    def messages(self, cid: str, *, branch: str = "active") -> list[dict[str, Any]]:
        where = "conversation_id = ?" + (" AND on_active_branch = 1" if branch == "active" else "")
        rows = self._read_all(f"SELECT * FROM messages WHERE {where} ORDER BY position, rid", (cid,))
        return [_message(r) for r in rows]

    def list(
        self,
        *,
        folder: str | None = None,
        tag: str | None = None,
        provider: str | None = None,
        collection: str = "all",
        project: str | None = None,
        sort: str = "newest",
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        where, params = self._filters(
            folder=folder, tag=tag, provider=provider, collection=collection, project=project
        )
        order = SORTS.get(sort, SORTS["newest"])
        base = f"FROM conversations c WHERE {where}"
        total = int(self._read_one(f"SELECT COUNT(*) AS n {base}", tuple(params))["n"])
        rows = self._read_all(
            f"SELECT c.* {base} ORDER BY c.pinned DESC, {order}, c.rid DESC LIMIT ? OFFSET ?",
            (*params, max(1, min(limit, 1000)), max(0, offset)),
        )
        return self._summaries(rows), total

    def _subtree(self, folder_id: str) -> list[str]:
        rows = self._read_all("SELECT id, parent_id FROM folders")
        children: dict[str | None, list[str]] = {}
        for r in rows:
            children.setdefault(r["parent_id"], []).append(r["id"])
        out, stack = [], [folder_id]
        while stack:
            f = stack.pop()
            if f in out:
                continue
            out.append(f)
            stack.extend(children.get(f, []))
        return out

    def _filters(
        self,
        *,
        folder: str | None = None,
        tag: str | None = None,
        provider: str | None = None,
        collection: str | None = "all",
        project: str | None = None,
        completeness: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> tuple[str, list[Any]]:
        """WHERE clause over ``conversations c``. ``collection=None`` applies no archive filter."""
        clauses: list[str] = []
        params: list[Any] = []
        if collection == "archived":
            clauses.append("c.archived = 1")
        elif collection is not None:
            clauses.append("c.archived = 0")
        if collection == "recent":
            cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
            clauses.append("(c.last_visited_at >= ? OR c.captured_at >= ?)")
            params += [cutoff, cutoff]
        elif collection == "uncategorized":
            clauses.append("NOT EXISTS (SELECT 1 FROM conversation_folders cf WHERE cf.conversation_id = c.id)")
        elif collection == "partial":
            clauses.append("c.completeness IN ('url_only', 'partial')")
        elif collection == "pinned":
            clauses.append("c.pinned = 1")
        elif collection == "favorites":
            clauses.append("c.favorite = 1")
        elif collection == "projects":
            clauses.append("c.remote_project_id IS NOT NULL")
        if folder:
            ids = self._subtree(folder)
            marks = ",".join("?" * len(ids))
            clauses.append(
                f"EXISTS (SELECT 1 FROM conversation_folders cf WHERE cf.conversation_id = c.id AND cf.folder_id IN ({marks}))"
            )
            params += ids
        if tag:
            clauses.append("EXISTS (SELECT 1 FROM conversation_tags ct WHERE ct.conversation_id = c.id AND ct.tag = ?)")
            params.append(clean_tag(tag))
        if provider:
            clauses.append("c.provider = ?")
            params.append(provider)
        if project:
            clauses.append("c.remote_project_id = ?")
            params.append(project)
        if completeness:
            clauses.append("c.completeness = ?")
            params.append(completeness)
        if since:
            clauses.append("COALESCE(c.updated_at, c.created_at) >= ?")
            params.append(since)
        if until:
            clauses.append("COALESCE(c.created_at, c.updated_at) <= ?")
            params.append(until)
        return " AND ".join(clauses) or "1", params

    # ── search ────────────────────────────────────────────────────────────────

    def search(
        self,
        q: str,
        *,
        folder: str | None = None,
        tag: str | None = None,
        provider: str | None = None,
        project: str | None = None,
        role: str | None = None,
        code_only: bool = False,
        completeness: str | None = None,
        since: str | None = None,
        until: str | None = None,
        conversation: str | None = None,
        include_archived: bool = True,
        limit: int = 30,
        hits_per_conversation: int = 3,
    ) -> dict[str, Any]:
        """Ranked search over titles and every indexed message.

        Returns ``{"query", "terms", "results": [summary + score + hits[]]}``; each hit carries
        the message id so a surface can open the exact message. Ranking is relevance (BM25,
        with a boost for a title match and for several matching messages), never recency.
        Raises :class:`navig_chats.query.EmptyQuery` for a query with nothing to look for.
        """
        parsed = fts_query.parse(q)
        where, params = self._filters(
            folder=folder, tag=tag, provider=provider, project=project, completeness=completeness,
            since=since, until=until, collection=None if include_archived else "all",
        )
        msg_where = [where]
        msg_params = list(params)
        if role:
            msg_where.append("m.role = ?")
            msg_params.append(role)
        if code_only:
            msg_where.append("m.has_code = 1")
        if conversation:
            msg_where.append("m.conversation_id = ?")
            msg_params.append(conversation)
        cap = 400 if not conversation else 2000
        # Phase 1 ranks with FTS5's own `rank` (cheap) and touches `conversations` only when a
        # filter needs it. snippet() is NOT computed here: in a sorted query SQLite evaluates
        # it for every match before the LIMIT applies, which dominated latency.
        join_c = " JOIN conversations c ON c.id = m.conversation_id" if where != "1" else ""
        base_sql = (
            "SELECT m.rid, m.conversation_id AS cid, m.mid, m.role, m.position, messages_fts.rank AS score"
            " FROM messages_fts JOIN messages m ON m.rid = messages_fts.rowid"
            f"{join_c} WHERE messages_fts MATCH ? AND {' AND '.join(msg_where)}"
        )
        args = (parsed.expression, *msg_params, cap)
        ranking = "relevance"
        try:
            msg_rows = self._read_budgeted(base_sql + " ORDER BY messages_fts.rank LIMIT ?", args)
        except _OverBudget:
            # A term so common that scoring every match would stall the surface: take the
            # newest matches instead (FTS5 streams rowid order and stops at the LIMIT), and
            # say so — the surface tells the person to add a word.
            ranking = "recent"
            msg_rows = self._read_all(base_sql + " ORDER BY messages_fts.rowid DESC LIMIT ?", args)
        title_rows: list[sqlite3.Row] = []
        if not (role or code_only or conversation):
            title_rows = self._read_all(
                "SELECT c.id AS cid, bm25(conversations_fts) AS score FROM conversations_fts"
                " JOIN conversations c ON c.rid = conversations_fts.rowid"
                f" WHERE conversations_fts MATCH ? AND {where} ORDER BY score LIMIT 200",
                (parsed.expression, *params),
            )
        agg: dict[str, dict[str, Any]] = {}
        for r in msg_rows:
            a = agg.setdefault(r["cid"], {"best": 0.0, "count": 0, "hits": [], "title": 0.0})
            rel = -float(r["score"])
            a["best"] = max(a["best"], rel)
            a["count"] += 1
            if len(a["hits"]) < (hits_per_conversation if not conversation else cap):
                a["hits"].append({"rid": r["rid"], "message_id": r["mid"], "role": r["role"], "position": r["position"]})
        for r in title_rows:
            a = agg.setdefault(r["cid"], {"best": 0.0, "count": 0, "hits": [], "title": 0.0})
            a["title"] = max(a["title"], -float(r["score"]))
        ranked = sorted(
            agg.items(),
            key=lambda kv: kv[1]["best"] + 0.5 * math.log1p(kv[1]["count"]) + 1.5 * kv[1]["title"] + (2.0 if kv[1]["title"] else 0.0),
            reverse=True,
        )[: max(1, limit)]
        ids = [cid for cid, _ in ranked]
        # Phase 2: snippets only for the hits that will be shown.
        shown = [h["rid"] for _, a in ranked for h in a["hits"]]
        snips: dict[int, str] = {}
        for chunk in _chunks(shown):  # type: ignore[arg-type]
            for r in self._read_all(
                "SELECT rowid, snippet(messages_fts, 0, char(2), char(3), '…', 24) AS snip FROM messages_fts"
                f" WHERE messages_fts MATCH ? AND rowid IN ({','.join('?' * len(chunk))})",
                (parsed.expression, *chunk),
            ):
                snips[r["rowid"]] = r["snip"]
        for _, a in ranked:
            for h in a["hits"]:
                h["snippet"] = snips.get(h.pop("rid"), "")
        rows = {r["id"]: r for chunk in _chunks(ids) for r in self._read_all(
            f"SELECT * FROM conversations c WHERE id IN ({','.join('?' * len(chunk))})", tuple(chunk)
        )}
        summaries = {s["id"]: s for s in self._summaries([rows[i] for i in ids if i in rows])}
        results = []
        for cid, a in ranked:
            if cid not in summaries:
                continue
            s = summaries[cid]
            hits = sorted(a["hits"], key=lambda h: h["position"]) if conversation else a["hits"]
            s.update(score=round(a["best"] + 1.5 * a["title"], 4), match_count=a["count"], title_match=bool(a["title"]), hits=hits)
            results.append(s)
        return {"query": q, "terms": parsed.terms, "results": results, "ranking": ranking}

    def _read_budgeted(self, sql: str, params: tuple) -> list[sqlite3.Row]:
        """``_read_all`` that gives up (``_OverBudget``) after :attr:`SEARCH_BUDGET_S`."""
        conn = self._get_conn()
        deadline = time.monotonic() + self.SEARCH_BUDGET_S
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
        try:
            return conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            if "interrupt" in str(exc).lower():
                raise _OverBudget from exc
            raise
        finally:
            conn.set_progress_handler(None, 0)

    # ── the person's organisation ─────────────────────────────────────────────

    def update(self, cid: str, **fields: Any) -> dict[str, Any]:
        """Set alias / pinned / favorite / archived / sort_key, and replace tags or folders."""
        with self.transaction() as conn:
            if conn.execute("SELECT 1 FROM conversations WHERE id = ?", (cid,)).fetchone() is None:
                raise NotFound(cid)
            sets, params = [], []
            if "alias" in fields:
                alias = (fields["alias"] or "").strip()[:300] or None
                sets.append("alias = ?")
                params.append(alias)
            if "brain" in fields:
                level = fields["brain"]
                if level not in (None, "", *BRAIN_LEVELS):
                    raise ValueError(f"brain must be one of {', '.join(BRAIN_LEVELS)} (or empty to inherit)")
                sets.append("brain = ?")
                params.append(level or None)
            for flag in ("pinned", "favorite", "archived"):
                if flag in fields:
                    sets.append(f"{flag} = ?")
                    params.append(_b(fields[flag]))
            if "sort_key" in fields:
                sets.append("sort_key = ?")
                params.append(None if fields["sort_key"] is None else float(fields["sort_key"]))
            if sets:
                conn.execute(f"UPDATE conversations SET {', '.join(sets)} WHERE id = ?", (*params, cid))
            if "tags" in fields:
                conn.execute("DELETE FROM conversation_tags WHERE conversation_id = ?", (cid,))
                self._add_tags(conn, [cid], fields["tags"] or [])
            if "folders" in fields:
                conn.execute("DELETE FROM conversation_folders WHERE conversation_id = ?", (cid,))
                self._place(conn, [cid], fields["folders"] or [])
        return self.get(cid)  # type: ignore[return-value]

    def _add_tags(self, conn: sqlite3.Connection, ids: list[str], tags: Iterable[str]) -> int:
        clean = sorted({t for t in (clean_tag(x) for x in tags) if t})
        n = 0
        for cid in ids:
            for t in clean:
                n += conn.execute(
                    "INSERT OR IGNORE INTO conversation_tags (conversation_id, tag) VALUES (?, ?)", (cid, t)
                ).rowcount
        return n

    def _place(self, conn: sqlite3.Connection, ids: list[str], folder_ids: Iterable[str]) -> int:
        n = 0
        now = _utcnow()
        for fid in folder_ids:
            if conn.execute("SELECT 1 FROM folders WHERE id = ?", (fid,)).fetchone() is None:
                raise NotFound(f"folder {fid}")
            for cid in ids:
                n += conn.execute(
                    "INSERT OR IGNORE INTO conversation_folders (conversation_id, folder_id, added_at) VALUES (?,?,?)",
                    (cid, fid, now),
                ).rowcount
        return n

    def bulk(self, ids: list[str], action: str, value: Any = None) -> int:
        """One change across many chats: ``move`` / ``add_to`` / ``remove_from`` a folder,
        ``tag`` / ``untag``, ``archive`` / ``unarchive``, ``pin`` / ``unpin``. Returns rows changed."""
        ids = [i for i in dict.fromkeys(ids) if i]
        if not ids:
            return 0
        with self.transaction() as conn:
            known = [
                r["id"] for chunk in _chunks(ids) for r in conn.execute(
                    f"SELECT id FROM conversations WHERE id IN ({','.join('?' * len(chunk))})", tuple(chunk)
                )
            ]
            if not known:
                return 0
            marks = ",".join("?" * len(known))
            if action == "move":
                conn.execute(f"DELETE FROM conversation_folders WHERE conversation_id IN ({marks})", tuple(known))
                return self._place(conn, known, [value] if value else [])
            if action == "add_to":
                return self._place(conn, known, [value])
            if action == "remove_from":
                return conn.execute(
                    f"DELETE FROM conversation_folders WHERE folder_id = ? AND conversation_id IN ({marks})",
                    (value, *known),
                ).rowcount
            if action == "tag":
                return self._add_tags(conn, known, value if isinstance(value, list) else [value])
            if action == "untag":
                tags = [clean_tag(t) for t in (value if isinstance(value, list) else [value])]
                tmarks = ",".join("?" * len(tags))
                return conn.execute(
                    f"DELETE FROM conversation_tags WHERE conversation_id IN ({marks}) AND tag IN ({tmarks})",
                    (*known, *tags),
                ).rowcount
            flags = {"archive": ("archived", 1), "unarchive": ("archived", 0), "pin": ("pinned", 1), "unpin": ("pinned", 0),
                     "favorite": ("favorite", 1), "unfavorite": ("favorite", 0)}
            if action in flags:
                col, v = flags[action]
                return conn.execute(f"UPDATE conversations SET {col} = ? WHERE id IN ({marks})", (v, *known)).rowcount
        raise ValueError(f"unknown action: {action}")

    def delete(self, cid: str) -> bool:
        """Remove a chat from the LIBRARY (never from the provider). Cascades its messages."""
        with self.transaction() as conn:
            return conn.execute("DELETE FROM conversations WHERE id = ?", (cid,)).rowcount > 0

    # ── folders ───────────────────────────────────────────────────────────────

    def folders(self) -> list[dict[str, Any]]:
        """Every folder with its direct count; the tree is built by the surface from parent_id."""
        rows = self._read_all(
            "SELECT f.*, (SELECT COUNT(*) FROM conversation_folders cf JOIN conversations c ON c.id = cf.conversation_id"
            " WHERE cf.folder_id = f.id AND c.archived = 0) AS count FROM folders f ORDER BY f.pinned DESC, f.sort_key, f.name COLLATE NOCASE"
        )
        return [
            {
                "id": r["id"], "parent_id": r["parent_id"], "name": r["name"], "color": r["color"], "icon": r["icon"],
                "pinned": bool(r["pinned"]), "sort_key": r["sort_key"], "count": int(r["count"]),
                "brain": r["brain"], "brain_project": r["brain_project"],
            }
            for r in rows
        ]

    def collection_counts(self) -> dict[str, int]:
        out = {}
        for name in COLLECTIONS:
            where, params = self._filters(collection=name)
            out[name] = int(self._read_one(f"SELECT COUNT(*) AS n FROM conversations c WHERE {where}", tuple(params))["n"])
        return out

    def create_folder(self, name: str, *, parent_id: str | None = None, color: str | None = None, icon: str | None = None) -> dict[str, Any]:
        name = (name or "").strip()[:120]
        if not name:
            raise ValueError("a folder needs a name")
        fid = new_id()
        now = _utcnow()
        with self.transaction() as conn:
            if parent_id and conn.execute("SELECT 1 FROM folders WHERE id = ?", (parent_id,)).fetchone() is None:
                raise NotFound(f"folder {parent_id}")
            top = conn.execute(
                "SELECT COALESCE(MAX(sort_key), 0) + 1 AS k FROM folders WHERE parent_id IS ?", (parent_id,)
            ).fetchone()["k"]
            conn.execute(
                "INSERT INTO folders (id, parent_id, name, color, icon, sort_key, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (fid, parent_id, name, color, icon, top, now, now),
            )
        return next(f for f in self.folders() if f["id"] == fid)

    def update_folder(self, fid: str, **fields: Any) -> dict[str, Any]:
        with self.transaction() as conn:
            if conn.execute("SELECT 1 FROM folders WHERE id = ?", (fid,)).fetchone() is None:
                raise NotFound(f"folder {fid}")
            sets, params = [], []
            if "name" in fields:
                name = (fields["name"] or "").strip()[:120]
                if not name:
                    raise ValueError("a folder needs a name")
                sets.append("name = ?")
                params.append(name)
            for key in ("color", "icon"):
                if key in fields:
                    sets.append(f"{key} = ?")
                    params.append(fields[key] or None)
            if "pinned" in fields:
                sets.append("pinned = ?")
                params.append(_b(fields["pinned"]))
            if "sort_key" in fields:
                sets.append("sort_key = ?")
                params.append(float(fields["sort_key"] or 0))
            if "brain" in fields:
                level = fields["brain"]
                if level not in (None, "", *BRAIN_LEVELS):
                    raise ValueError(f"brain must be one of {', '.join(BRAIN_LEVELS)} (or empty to inherit)")
                sets.append("brain = ?")
                params.append(level or None)
            if "brain_project" in fields:
                sets.append("brain_project = ?")
                params.append((str(fields["brain_project"] or "").strip()[:200]) or None)
            if "parent_id" in fields:
                parent = fields["parent_id"] or None
                if parent is not None:
                    if conn.execute("SELECT 1 FROM folders WHERE id = ?", (parent,)).fetchone() is None:
                        raise NotFound(f"folder {parent}")
                    if parent in self._subtree(fid):
                        raise ValueError("a folder cannot move inside itself")
                sets.append("parent_id = ?")
                params.append(parent)
            if sets:
                sets.append("updated_at = ?")
                params.append(_utcnow())
                conn.execute(f"UPDATE folders SET {', '.join(sets)} WHERE id = ?", (*params, fid))
        return next(f for f in self.folders() if f["id"] == fid)

    def delete_folder(self, fid: str) -> bool:
        """Delete a folder. Its chats stay in the library; its subfolders move up one level."""
        with self.transaction() as conn:
            row = conn.execute("SELECT parent_id FROM folders WHERE id = ?", (fid,)).fetchone()
            if row is None:
                return False
            conn.execute("UPDATE folders SET parent_id = ? WHERE parent_id = ?", (row["parent_id"], fid))
            conn.execute("DELETE FROM folders WHERE id = ?", (fid,))
            return True

    def project_counts(self) -> list[dict[str, Any]]:
        """ChatGPT Projects seen in the library (read-only: set in ChatGPT, never by NAVIG)."""
        rows = self._read_all(
            "SELECT remote_project_id AS id, MAX(remote_project_name) AS name, COUNT(*) AS n FROM conversations"
            " WHERE remote_project_id IS NOT NULL AND archived = 0 GROUP BY remote_project_id ORDER BY n DESC"
        )
        return [{"id": r["id"], "name": r["name"], "count": int(r["n"])} for r in rows]

    def tag_counts(self) -> dict[str, int]:
        return {
            r["tag"]: int(r["n"])
            for r in self._read_all("SELECT tag, COUNT(*) AS n FROM conversation_tags GROUP BY tag ORDER BY n DESC, tag")
        }

    # ── bookmarks ─────────────────────────────────────────────────────────────

    def add_bookmark(self, cid: str, *, message_id: str | None, kind: str = "bookmark", note: str | None = None, quote: str | None = None) -> dict[str, Any]:
        if kind not in BOOKMARK_KINDS:
            raise ValueError(f"kind must be one of {', '.join(BOOKMARK_KINDS)}")
        bid = new_id()
        with self.transaction() as conn:
            if conn.execute("SELECT 1 FROM conversations WHERE id = ?", (cid,)).fetchone() is None:
                raise NotFound(cid)
            conn.execute(
                "INSERT INTO bookmarks (id, conversation_id, message_id, kind, note, quote, created_at) VALUES (?,?,?,?,?,?,?)",
                (bid, cid, message_id, kind, (note or "")[:4000] or None, (quote or "")[:4000] or None, _utcnow()),
            )
        return next(b for b in self.bookmarks(cid) if b["id"] == bid)

    def remove_bookmark(self, bid: str) -> bool:
        with self.transaction() as conn:
            return conn.execute("DELETE FROM bookmarks WHERE id = ?", (bid,)).rowcount > 0

    def bookmarks(self, cid: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM bookmarks" + (" WHERE conversation_id = ?" if cid else "") + " ORDER BY created_at DESC"
        return [dict(r) for r in self._read_all(sql, (cid,) if cid else ())]

    # ── import jobs ───────────────────────────────────────────────────────────

    def start_import(self, source: str) -> str:
        jid = new_id()
        self._write(
            "INSERT INTO imports (id, source, status, started_at) VALUES (?, ?, 'running', ?)", (jid, source, _utcnow())
        )
        return jid

    def update_import(self, jid: str, **fields: Any) -> None:
        cols = {k: v for k, v in fields.items() if k in {"status", "total", "done", "added", "updated", "unchanged", "finished_at"}}
        if "errors" in fields:
            cols["errors_json"] = json.dumps(fields["errors"][-200:], ensure_ascii=False)
        if not cols:
            return
        self._write(
            f"UPDATE imports SET {', '.join(f'{k} = ?' for k in cols)} WHERE id = ?", (*cols.values(), jid)
        )

    def get_import(self, jid: str) -> dict[str, Any] | None:
        r = self._read_one("SELECT * FROM imports WHERE id = ?", (jid,))
        if r is None:
            return None
        out = dict(r)
        out["errors"] = json.loads(out.pop("errors_json") or "[]")
        return out

    def stats(self) -> dict[str, Any]:
        r = self._read_one(
            "SELECT COUNT(*) AS conversations, COALESCE(SUM(message_count), 0) AS messages,"
            " SUM(completeness = 'complete') AS complete, SUM(completeness = 'rendered') AS rendered,"
            " SUM(completeness IN ('partial', 'url_only')) AS partial FROM conversations"
        )
        return {k: int(r[k] or 0) for k in ("conversations", "messages", "complete", "rendered", "partial")} | {
            "folders": int(self._read_one("SELECT COUNT(*) AS n FROM folders")["n"]),
            "db_path": str(self.db_path),
        }

    def rebuild_index(self) -> None:
        """Rebuild both FTS indexes from their content tables (recovery after corruption)."""
        with self.transaction() as conn:
            conn.execute("INSERT INTO messages_fts(messages_fts) VALUES ('rebuild')")
            conn.execute("INSERT INTO conversations_fts(conversations_fts) VALUES ('rebuild')")

    def integrity(self) -> bool:
        """True when both FTS indexes agree with their content tables."""
        try:
            conn = self._get_conn()
            conn.execute("INSERT INTO messages_fts(messages_fts, rank) VALUES ('integrity-check', 1)")
            conn.execute("INSERT INTO conversations_fts(conversations_fts, rank) VALUES ('integrity-check', 1)")
            return True
        except sqlite3.DatabaseError:
            return False


# ── sharing with NAVIG's knowledge index ──────────────────────────────────────


def _folder_levels(folders: list[dict[str, Any]]) -> dict[str, tuple[str, str | None]]:
    """``folder id -> (effective level, effective project)``. A folder with no level of its
    own inherits its parent's; the top level defaults to ``off``."""
    by_id = {f["id"]: f for f in folders}
    out: dict[str, tuple[str, str | None]] = {}

    def resolve(fid: str, guard: int = 0) -> tuple[str, str | None]:
        if fid in out:
            return out[fid]
        f = by_id.get(fid)
        if f is None or guard > 64:
            return ("off", None)
        parent = resolve(f["parent_id"], guard + 1) if f["parent_id"] else ("off", None)
        level = f.get("brain") or parent[0]
        project = f.get("brain_project") or parent[1]
        out[fid] = (level, project)
        return out[fid]

    for fid in by_id:
        resolve(fid)
    return out


def effective_brain(conv: dict[str, Any], levels: dict[str, tuple[str, str | None]]) -> tuple[str, str | None]:
    """How far one chat may travel, and which NAVIG project it belongs to there.

    The chat's own setting wins. Otherwise the MOST RESTRICTIVE of its folders applies, so a
    chat filed in both a shared and a private folder stays private. Unfiled: ``off``.
    """
    folder_levels = [levels.get(f, ("off", None)) for f in conv.get("folders") or []]
    project = next((p for _, p in folder_levels if p), None)
    if conv.get("brain"):
        return conv["brain"], project
    if not folder_levels:
        return "off", project
    return min((lvl for lvl, _ in folder_levels), key=lambda lvl: _BRAIN_RANK.get(lvl, 0)), project


def brain_shared(store: "ChatStore", *, min_level: str = "local") -> list[dict[str, Any]]:
    """Every chat shared at ``min_level`` or beyond, with its level and project."""
    levels = _folder_levels(store.folders())
    want = _BRAIN_RANK[min_level]
    out = []
    offset = 0
    while True:
        rows, total = store.list(collection=None, sort="oldest", limit=1000, offset=offset)
        for r in rows:
            level, project = effective_brain(r, levels)
            if _BRAIN_RANK.get(level, 0) >= want:
                out.append({**r, "brain_level": level, "brain_project": project})
        offset += len(rows)
        if not rows or offset >= total:
            return out


# ── row shaping ───────────────────────────────────────────────────────────────


def _chunks(items: list[str], size: int = 400) -> Iterator[list[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _summary(r: sqlite3.Row, folders: list[str], tags: list[str]) -> dict[str, Any]:
    return {
        "id": r["id"],
        "provider": r["provider"],
        "native_id": r["native_id"],
        "url": r["url"],
        "title": r["title"],
        "alias": r["alias"],
        "display_title": (r["alias"] or r["title"] or "Untitled chat"),
        "remote_project_id": r["remote_project_id"],
        "remote_project_name": r["remote_project_name"],
        "gizmo_id": r["gizmo_id"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
        "last_visited_at": r["last_visited_at"],
        "captured_at": r["captured_at"],
        "completeness": r["completeness"],
        "provenance": r["provenance"],
        "revision": r["revision"],
        "message_count": r["message_count"],
        "archived": bool(r["archived"]),
        "archived_remote": bool(r["archived_remote"]),
        "pinned": bool(r["pinned"]),
        "favorite": bool(r["favorite"]),
        "sort_key": r["sort_key"],
        "brain": r["brain"],
        "folders": folders,
        "tags": tags,
    }


def _message(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": r["mid"],
        "parent_id": r["parent_id"],
        "role": r["role"],
        "content_type": r["content_type"],
        "text": r["text"],
        "parts": json.loads(r["parts_json"]) if r["parts_json"] else None,
        "attachments": json.loads(r["attachments_json"]) if r["attachments_json"] else [],
        "has_code": bool(r["has_code"]),
        "created_at": r["created_at"],
        "model": r["model"],
        "provenance": r["provenance"],
        "on_active_branch": bool(r["on_active_branch"]),
        "position": r["position"],
    }


__all__ = [
    "COLLECTIONS",
    "COMPLETENESS",
    "MARK_END",
    "MARK_START",
    "SORTS",
    "ChatStore",
    "NotFound",
    "clean_tag",
    "default_db_path",
]
