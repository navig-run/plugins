"""The notes store — ``<navig data dir>/notes.db``.

The NAVIG Dock is where notes are written; the browser keeps its own copy and pushes changes
here (``POST /api/deck/notes/sync``). Other surfaces — this CLI, the agent, a Space — may also
write. Every note carries a revision (``rev``) that each writer bumps, so a sync is a plain
merge: **the higher revision wins**, an equal revision with a later ``updatedAt`` wins, and a
note is never overwritten by an older copy. Deletes are tombstones (``deleted_at``) so they
travel to every copy; nothing is purged here except by an explicit ``purge``.

Every write bumps a store-wide change counter (``seq``); a client pulls "everything since my
cursor" and gets exactly the rows that changed, tombstones included.

The note itself is kept verbatim as JSON (``data``) — unknown fields from a newer Dock survive
a round trip — with the searchable parts copied into columns and an FTS5 index.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from navig_sdk.store import BaseStore

DB_FILENAME = "notes.db"

SCOPES = ("page", "site", "global")
STATES = ("open", "minimized", "hidden")
#: How far a note may travel beyond this store. ``off``: the Dock does not sync at all (never
#: reaches here). ``local``: NAVIG's own Recall index on this computer — never sent anywhere.
#: ``agent``: also allowed into agent context, which goes to the AI provider the person chose.
SHARE_LEVELS = ("local", "agent")
ORIGINS = ("dock", "cli", "agent", "space", "import")

LIMITS = {"id": 80, "title": 200, "body": 100_000, "tags": 20, "tag": 40, "url": 2048, "key": 2048, "json": 262_144}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
  id            TEXT PRIMARY KEY,
  rev           INTEGER NOT NULL,
  updated_at    INTEGER NOT NULL,           -- ms, the writer's clock
  created_at    INTEGER NOT NULL,
  deleted_at    INTEGER,                    -- ms; a tombstone when set
  scope         TEXT NOT NULL,
  page_key      TEXT NOT NULL DEFAULT '',
  site_key      TEXT NOT NULL DEFAULT '',
  source_url    TEXT NOT NULL DEFAULT '',
  source_title  TEXT NOT NULL DEFAULT '',
  title         TEXT NOT NULL DEFAULT '',
  body          TEXT NOT NULL DEFAULT '',
  tags          TEXT NOT NULL DEFAULT '',   -- space-joined, for FTS and filters
  state         TEXT NOT NULL DEFAULT 'open',
  remind_at     INTEGER,
  origin        TEXT NOT NULL DEFAULT 'dock',
  share         TEXT NOT NULL DEFAULT 'local',
  seq           INTEGER NOT NULL,           -- store-wide change counter at the last write
  data          TEXT NOT NULL               -- the note as the writer sent it (JSON)
);
CREATE INDEX IF NOT EXISTS notes_seq ON notes(seq);
CREATE INDEX IF NOT EXISTS notes_site ON notes(site_key) WHERE deleted_at IS NULL;
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
  title, body, tags, source_title, site_key,
  content='notes', content_rowid='rowid', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
  INSERT INTO notes_fts(rowid, title, body, tags, source_title, site_key)
  VALUES (new.rowid, new.title, new.body, new.tags, new.source_title, new.site_key);
END;
CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
  INSERT INTO notes_fts(notes_fts, rowid, title, body, tags, source_title, site_key)
  VALUES ('delete', old.rowid, old.title, old.body, old.tags, old.source_title, old.site_key);
END;
CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
  INSERT INTO notes_fts(notes_fts, rowid, title, body, tags, source_title, site_key)
  VALUES ('delete', old.rowid, old.title, old.body, old.tags, old.source_title, old.site_key);
  INSERT INTO notes_fts(rowid, title, body, tags, source_title, site_key)
  VALUES (new.rowid, new.title, new.body, new.tags, new.source_title, new.site_key);
END;
"""


class NoteError(ValueError):
    """A note that cannot be stored (missing id, wrong shape, too large)."""


def default_db_path() -> Path:
    """``$NAVIG_NOTES_DB`` when set, else ``<navig data dir>/notes.db``."""
    override = os.environ.get("NAVIG_NOTES_DB", "").strip()
    if override:
        return Path(override)
    from navig_sdk.host import data_dir  # noqa: PLC0415 - resolved per call, never at import

    return data_dir() / DB_FILENAME


def now_ms() -> int:
    return int(time.time() * 1000)


def _int(v: Any, default: int | None) -> int | None:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return n


def _str(v: Any, limit: int) -> str:
    if v is None:
        return ""
    return (v if isinstance(v, str) else str(v))[:limit]


def clean_tags(tags: Any) -> list[str]:
    raw = tags if isinstance(tags, list) else str(tags or "").split(",")
    out: list[str] = []
    for t in raw:
        tag = _str(t, LIMITS["tag"]).strip().lstrip("#").lower()
        if tag and tag not in out:
            out.append(tag)
        if len(out) >= LIMITS["tags"]:
            break
    return out


def normalize(raw: Any) -> dict[str, Any]:
    """Validate one incoming note (the Dock's v2 shape). Unknown fields are KEPT."""
    if not isinstance(raw, dict):
        raise NoteError("a note must be an object")
    nid = _str(raw.get("id"), LIMITS["id"]).strip()
    if not nid or not re.fullmatch(r"[A-Za-z0-9_.:-]+", nid):
        raise NoteError("a note needs an id of letters, digits, '-', '_', '.' or ':'")
    scope = raw.get("scope") if raw.get("scope") in SCOPES else "page"
    page_key = _str(raw.get("pageKey"), LIMITS["key"])
    site_key = _str(raw.get("siteKey"), LIMITS["key"])
    if scope == "page" and not page_key:
        raise NoteError(f"note {nid}: a page note needs its page")
    if scope == "site" and not site_key:
        raise NoteError(f"note {nid}: a site note needs its site")
    created = _int(raw.get("createdAt"), None) or now_ms()
    note = dict(raw)
    note.update({
        "id": nid,
        "scope": scope,
        "pageKey": page_key,
        "siteKey": site_key,
        "title": _str(raw.get("title"), LIMITS["title"]),
        "body": _str(raw.get("body"), LIMITS["body"]),
        "sourceUrl": _str(raw.get("sourceUrl"), LIMITS["url"]),
        "sourceTitle": _str(raw.get("sourceTitle"), 300),
        "tags": clean_tags(raw.get("tags")),
        "state": raw.get("state") if raw.get("state") in STATES else "open",
        "createdAt": created,
        "updatedAt": _int(raw.get("updatedAt"), None) or created,
        "rev": max(1, _int(raw.get("rev"), 1) or 1),
    })
    note.pop("deletedAt", None)  # a live note; deletes travel as tombstones
    if len(json.dumps(note, ensure_ascii=False)) > LIMITS["json"]:
        raise NoteError(f"note {nid} is too large")
    return note


def display_title(note: dict[str, Any]) -> str:
    if (note.get("title") or "").strip():
        return note["title"].strip()
    for line in (note.get("body") or "").splitlines():
        line = line.strip().lstrip("#>*- ").strip()
        if line:
            return line[:80]
    src = note.get("sourceTitle") or ""
    return f"Note — {src[:60]}" if src else "Untitled note"


def _row_to_note(row: sqlite3.Row) -> dict[str, Any]:
    note = json.loads(row["data"])
    note["origin"] = row["origin"]
    note["share"] = row["share"]
    if row["deleted_at"]:
        note["deletedAt"] = row["deleted_at"]
    return note


def match_query(q: str) -> str:
    """User words → a safe FTS5 query: every word must match (prefix), quotes keep phrases."""
    parts = re.findall(r'"([^"]+)"|(\S+)', q or "")
    terms = []
    for phrase, word in parts:
        if phrase:
            terms.append('"' + phrase.replace('"', "") + '"')
        else:
            w = re.sub(r"[^\w#.-]", "", word).lstrip("#")
            if w:
                terms.append('"' + w.replace('"', "") + '"*')
    return " ".join(terms)


class NoteStore(BaseStore):
    SCHEMA_VERSION = 1

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(_SCHEMA)

    def _migrate(self, conn: sqlite3.Connection, from_version: int, to_version: int) -> None:  # noqa: ARG002
        return  # v1 is the first schema

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

    # ── change counter ────────────────────────────────────────────────────────

    @staticmethod
    def _next_seq(conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT value FROM kv WHERE key = 'seq'").fetchone()
        seq = int(row["value"]) + 1 if row else 1
        conn.execute("INSERT INTO kv(key, value) VALUES ('seq', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(seq),))
        return seq

    def cursor(self) -> int:
        row = self._get_conn().execute("SELECT value FROM kv WHERE key = 'seq'").fetchone()
        return int(row["value"]) if row else 0

    def set_meta(self, key: str, value: Any) -> None:
        with self.transaction() as conn:
            conn.execute("INSERT INTO kv(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                         (key, json.dumps(value)))

    def meta(self, key: str, default: Any = None) -> Any:
        row = self._get_conn().execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    # ── writes ────────────────────────────────────────────────────────────────

    def _write(self, conn: sqlite3.Connection, note: dict[str, Any], *, origin: str, share: str, deleted_at: int | None,
               exists: bool) -> None:
        seq = self._next_seq(conn)
        cols = (
            note["rev"], note["updatedAt"], note["createdAt"], deleted_at, note["scope"], note["pageKey"], note["siteKey"],
            note["sourceUrl"], note["sourceTitle"], note["title"], note["body"], " ".join(note["tags"]), note["state"],
            _int(note.get("remindAt"), None), origin, share, seq, json.dumps(note, ensure_ascii=False),
        )
        if exists:
            conn.execute(
                "UPDATE notes SET rev=?, updated_at=?, created_at=?, deleted_at=?, scope=?, page_key=?, site_key=?, source_url=?,"
                " source_title=?, title=?, body=?, tags=?, state=?, remind_at=?, origin=?, share=?, seq=?, data=? WHERE id=?",
                (*cols, note["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO notes (rev, updated_at, created_at, deleted_at, scope, page_key, site_key, source_url, source_title,"
                " title, body, tags, state, remind_at, origin, share, seq, data, id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (*cols, note["id"]),
            )

    @staticmethod
    def _newer(rev: int, updated: int, row: sqlite3.Row) -> bool:
        return rev > row["rev"] or (rev == row["rev"] and updated > row["updated_at"])

    def upsert(self, raw: Any, *, origin: str = "dock", share: str = "local",
               conn: sqlite3.Connection | None = None) -> str:
        """Merge one note. Returns ``added`` · ``updated`` · ``unchanged`` · ``stale``.

        ``stale``: this store already holds a newer copy (a higher revision, or the same one
        written later) — nothing changes, and the caller should take the stored copy.
        """
        if conn is None:
            with self.transaction() as c:
                return self.upsert(raw, origin=origin, share=share, conn=c)
        note = normalize(raw)
        origin = origin if origin in ORIGINS else "dock"
        share = share if share in SHARE_LEVELS else "local"
        row = conn.execute("SELECT rev, updated_at, deleted_at, data, share FROM notes WHERE id = ?", (note["id"],)).fetchone()
        if row is None:
            self._write(conn, note, origin=origin, share=share, deleted_at=None, exists=False)
            return "added"
        if not self._newer(note["rev"], note["updatedAt"], row):
            if note["rev"] == row["rev"] and not row["deleted_at"] and row["share"] != share:
                # Same note, only the sharing level moved (the Library setting changed).
                conn.execute("UPDATE notes SET share = ?, seq = ? WHERE id = ?", (share, self._next_seq(conn), note["id"]))
                return "updated"
            # Exactly what is stored → unchanged. Anything older (lower revision, or the same one
            # written earlier) → stale, and the sync hands the client the stored copy.
            same = note["rev"] == row["rev"] and note["updatedAt"] == row["updated_at"] and not row["deleted_at"]
            return "unchanged" if same else "stale"
        self._write(conn, note, origin=origin, share=share, deleted_at=None, exists=True)
        return "updated"

    def tombstone(self, nid: str, rev: int, deleted_at: int | None = None, *,
                  conn: sqlite3.Connection | None = None) -> str:
        """Mark a note deleted. Returns ``deleted`` · ``unchanged`` · ``stale`` · ``missing``."""
        if conn is None:
            with self.transaction() as c:
                return self.tombstone(nid, rev, deleted_at, conn=c)
        row = conn.execute("SELECT rev, deleted_at, data FROM notes WHERE id = ?", (nid,)).fetchone()
        if row is None:
            return "missing"
        if row["deleted_at"]:
            return "unchanged"
        if rev < row["rev"]:
            return "stale"  # edited here after the deleting copy last saw it
        conn.execute("UPDATE notes SET deleted_at = ?, seq = ? WHERE id = ?",
                     (int(deleted_at or now_ms()), self._next_seq(conn), nid))
        return "deleted"

    def sync(self, notes: list[Any], deleted: list[Any], *, since: int = 0, share: str = "local",
             origin: str = "dock") -> dict[str, Any]:
        """One round of a client sync, atomically: apply its changes, return everyone else's.

        The answer's ``changes`` are every row written after ``since`` that the client does not
        already hold at that exact revision — rows it just pushed are left out, a stored copy
        that beat one of its pushes is put in.
        """
        results = {"added": 0, "updated": 0, "unchanged": 0, "stale": 0, "deleted": 0, "rejected": []}
        pushed: dict[str, int] = {}
        with self.transaction() as conn:
            for raw in notes:
                try:
                    r = self.upsert(raw, origin=origin, share=share, conn=conn)
                except NoteError as exc:
                    results["rejected"].append({"id": str((raw or {}).get("id", ""))[:80] if isinstance(raw, dict) else "",
                                                "error": str(exc)})
                    continue
                results[r] += 1
                if r != "stale":
                    pushed[str(raw["id"])] = int(raw.get("rev") or 1)
            for d in deleted:
                if not isinstance(d, dict) or not d.get("id"):
                    continue
                r = self.tombstone(str(d["id"])[:80], _int(d.get("rev"), 0) or 0, _int(d.get("deletedAt"), None), conn=conn)
                if r == "deleted":
                    results["deleted"] += 1
                    pushed[str(d["id"])] = -1
            rows = conn.execute("SELECT * FROM notes WHERE seq > ? ORDER BY seq", (int(since or 0),)).fetchall()
            cursor = self.cursor_in(conn)
        changes = []
        for row in rows:
            mine = pushed.get(row["id"])
            if mine is not None and (mine == -1 and row["deleted_at"] or mine == row["rev"] and not row["deleted_at"]):
                continue
            changes.append({"id": row["id"], "rev": row["rev"], "deleted": True, "deletedAt": row["deleted_at"]}
                           if row["deleted_at"] else _row_to_note(row))
        return {"cursor": cursor, "results": results, "changes": changes}

    @staticmethod
    def cursor_in(conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT value FROM kv WHERE key = 'seq'").fetchone()
        return int(row["value"]) if row else 0

    def create(self, *, body: str, title: str = "", url: str = "", scope: str = "global", tags: Any = None,
               origin: str = "cli", share: str = "local", page_key: str = "", site_key: str = "") -> dict[str, Any]:
        """A note written here (CLI, agent, a Space) — the Dock picks it up on its next sync."""
        if scope not in SCOPES:
            raise NoteError(f"scope must be one of {', '.join(SCOPES)}")
        if scope in ("page", "site") and not (page_key if scope == "page" else site_key):
            keys = keys_for_url(url)
            if not keys:
                raise NoteError(f"a {scope} note needs a web address (--url)")
            page_key, site_key = keys["pageKey"], keys["siteKey"]
        ts = now_ms()
        note = {
            "id": uuid.uuid4().hex, "schema": 2, "title": title, "body": body, "scope": scope,
            "pageKey": page_key, "siteKey": site_key, "sourceUrl": url, "sourceTitle": "",
            "color": "amber", "tags": clean_tags(tags), "state": "open", "geom": None, "z": 0,
            "createdAt": ts, "updatedAt": ts, "rev": 1,
        }
        self.upsert(note, origin=origin, share=share)
        got = self.get(note["id"])
        assert got is not None
        return got

    def delete(self, nid: str) -> str:
        """Delete from this side (CLI/agent): a tombstone one revision ahead, so it wins."""
        with self.transaction() as conn:
            row = conn.execute("SELECT rev, deleted_at, data FROM notes WHERE id = ?", (nid,)).fetchone()
            if row is None:
                return "missing"
            if row["deleted_at"]:
                return "unchanged"
            note = json.loads(row["data"])
            note["rev"] = row["rev"] + 1
            note["updatedAt"] = now_ms()
            seq = self._next_seq(conn)
            conn.execute("UPDATE notes SET deleted_at = ?, rev = ?, updated_at = ?, data = ?, seq = ? WHERE id = ?",
                         (note["updatedAt"], note["rev"], note["updatedAt"], json.dumps(note, ensure_ascii=False), seq, nid))
            return "deleted"

    def purge(self, older_than_days: int = 30) -> int:
        """Drop tombstones older than N days (every copy has long since seen them)."""
        cutoff = now_ms() - older_than_days * 86_400_000
        with self.transaction() as conn:
            return conn.execute("DELETE FROM notes WHERE deleted_at IS NOT NULL AND deleted_at < ?", (cutoff,)).rowcount

    # ── reads ─────────────────────────────────────────────────────────────────

    def get(self, nid: str, *, include_deleted: bool = False) -> dict[str, Any] | None:
        row = self._get_conn().execute("SELECT * FROM notes WHERE id = ?", (nid,)).fetchone()
        if row is None or (row["deleted_at"] and not include_deleted):
            return None
        return _row_to_note(row)

    def resolve(self, ref: str) -> dict[str, Any] | None:
        """An id, or a unique id prefix (as the CLI prints them)."""
        ref = (ref or "").strip()
        if not ref:
            return None
        hit = self.get(ref)
        if hit:
            return hit
        rows = self._get_conn().execute(
            "SELECT * FROM notes WHERE id LIKE ? AND deleted_at IS NULL LIMIT 2", (ref.replace("%", "") + "%",)).fetchall()
        return _row_to_note(rows[0]) if len(rows) == 1 else None

    def list(self, *, site: str | None = None, scope: str | None = None, tag: str | None = None,
             limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        where = ["deleted_at IS NULL"]
        args: list[Any] = []
        if site:
            where.append("site_key = ?")
            args.append(site)
        if scope:
            where.append("scope = ?")
            args.append(scope)
        if tag:
            where.append("(' ' || tags || ' ') LIKE ?")
            args.append(f"% {tag.lower().lstrip('#')} %")
        rows = self._get_conn().execute(
            f"SELECT * FROM notes WHERE {' AND '.join(where)} ORDER BY updated_at DESC LIMIT ? OFFSET ?",  # noqa: S608 — fixed clauses
            (*args, int(limit), int(offset)),
        ).fetchall()
        return [_row_to_note(r) for r in rows]

    def search(self, q: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """Full-text search over title, text, tags, page title and site; best matches first."""
        mq = match_query(q)
        if not mq:
            return []
        rows = self._get_conn().execute(
            "SELECT n.*, snippet(notes_fts, 1, char(2), char(3), '…', 12) AS snip FROM notes_fts"
            " JOIN notes n ON n.rowid = notes_fts.rowid"
            " WHERE notes_fts MATCH ? AND n.deleted_at IS NULL ORDER BY rank LIMIT ?",
            (mq, int(limit)),
        ).fetchall()
        out = []
        for r in rows:
            note = _row_to_note(r)
            note["snippet"] = r["snip"] or ""
            out.append(note)
        return out

    def shared(self, min_level: str = "local") -> list[dict[str, Any]]:
        """Live notes at or above a sharing level (``agent`` ⊂ ``local``) — what Recall may read."""
        levels = ("agent",) if min_level == "agent" else SHARE_LEVELS
        rows = self._get_conn().execute(
            f"SELECT * FROM notes WHERE deleted_at IS NULL AND share IN ({','.join('?' * len(levels))}) ORDER BY updated_at DESC",  # noqa: S608
            levels,
        ).fetchall()
        return [_row_to_note(r) for r in rows]

    def stats(self) -> dict[str, Any]:
        c = self._get_conn()
        row = c.execute(
            "SELECT COUNT(*) AS live, SUM(scope='page') AS page, SUM(scope='site') AS site, SUM(scope='global') AS everywhere,"
            " COUNT(DISTINCT CASE WHEN scope != 'global' THEN site_key END) AS sites, SUM(share='agent') AS agent,"
            " MAX(updated_at) AS last_edit FROM notes WHERE deleted_at IS NULL"
        ).fetchone()
        dead = c.execute("SELECT COUNT(*) FROM notes WHERE deleted_at IS NOT NULL").fetchone()[0]
        return {
            "notes": row["live"] or 0, "page": row["page"] or 0, "site": row["site"] or 0,
            "everywhere": row["everywhere"] or 0, "sites": row["sites"] or 0, "agent": row["agent"] or 0,
            "deleted": dead, "last_edit": row["last_edit"], "cursor": self.cursor(),
            "last_sync": self.meta("last_sync"), "db": str(self.db_path),
        }


# ── URL keys (the server-side twin of the Dock's shared/notes-url.js, for CLI-made notes) ─────

_TRACKING = re.compile(r"^(utm_.*|fbclid|gclid|dclid|msclkid|mc_cid|mc_eid|igshid|ref_src|si|_ga|_gl)$", re.I)
_SECRET = re.compile(r"^(token|access_token|refresh_token|id_token|auth.*|code|state|nonce|key|api_?key|password|passwd|pwd|"
                     r"secret|client_secret|jwt|sid|session.*|sig|signature|otp|ticket|x-amz-.*|oauth_.*)$", re.I)


def keys_for_url(url: str) -> dict[str, str] | None:
    """Page and site keys for a typed web address — the Dock's rules for the common cases.

    The Dock recognises more site profiles (YouTube, GitHub, Reddit, Google Docs); a note made
    here for one of those pages gets the generic key, and the Dock re-keys nothing. Use the
    Dock to make page notes on those sites, or a site/everywhere note here.
    """
    from urllib.parse import parse_qsl, urlencode, urlsplit  # noqa: PLC0415

    try:
        u = urlsplit(url.strip())
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not u.hostname:
        return None
    host = u.hostname.lower()
    if host.startswith("www.") and host.count(".") >= 2:
        host = host[4:]
    if u.port and not ((u.scheme == "https" and u.port == 443) or (u.scheme == "http" and u.port == 80)):
        host = f"{host}:{u.port}"
    path = u.path.rstrip("/") or "/"
    q = sorted((k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if not _TRACKING.match(k) and not _SECRET.match(k))
    frag = u.fragment if re.match(r"^!?/", u.fragment or "") else ""
    page = f"{host}{path}{('?' + urlencode(q)) if q else ''}{('#' + frag) if frag and len(frag) > 1 else ''}"
    return {"pageKey": page, "siteKey": host}


__all__ = ["NoteError", "NoteStore", "SCOPES", "SHARE_LEVELS", "default_db_path", "display_title", "keys_for_url",
           "match_query", "normalize"]
