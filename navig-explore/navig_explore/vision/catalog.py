"""SQLite catalog for the vision pass — keyed on CONTENT HASH, not path.

Why content-keyed: every off-the-shelf photo manager keys its person-tags and
albums by file path, so the moment you reorganise the library every label is
lost. This package *proposes reorganisations*, so path-keying would destroy the
labels it just produced. ``files`` is the only path-keyed table; every derived
fact hangs off ``sha256`` and is re-found automatically after a move.

Vectors live as fp16 BLOBs and are searched with a plain numpy matmul. At
~100k photos the whole embedding matrix is ~100 MB — it fits in RAM, the search
is exact, and it costs zero dependencies. An ANN index (sqlite-vec, FAISS,
LanceDB) buys nothing at this scale and sqlite-vec is still pre-1.0 with
warned-about breaking storage changes.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 2
DB_NAME = "vision.db"

# Feature tables are keyed by sha256; dropping a file's rows is a single join away.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path        TEXT PRIMARY KEY,   -- normcase absolute path
    root        TEXT NOT NULL,
    rel         TEXT NOT NULL,
    name        TEXT NOT NULL,
    ext         TEXT NOT NULL,
    size        INTEGER NOT NULL,
    mtime       REAL NOT NULL,
    sha256      TEXT,               -- NULL until the decode pass hashes it
    present     INTEGER NOT NULL DEFAULT 1,
    first_seen  TEXT NOT NULL,
    p_ok        INTEGER,            -- probe columns, ingested from meta.jsonl
    p_w         INTEGER,
    p_h         INTEGER,
    p_camera    TEXT,
    p_created   TEXT,
    p_gps_lat   REAL,
    p_gps_lon   REAL,
    p_res_tier  TEXT,
    p_source_class TEXT
);
CREATE INDEX IF NOT EXISTS files_sha  ON files(sha256);
CREATE INDEX IF NOT EXISTS files_root ON files(root);

-- One row per distinct *content*, whatever paths point at it.
CREATE TABLE IF NOT EXISTS assets (
    sha256      TEXT PRIMARY KEY,
    bytes       INTEGER,
    w           INTEGER,
    h           INTEGER,
    decoded     INTEGER NOT NULL DEFAULT 0,  -- 0 = undecodable bytes, and we say so
    fail_reason TEXT,
    upright     INTEGER,          -- degrees of rotation that make it upright
    quality     REAL,             -- variance-of-Laplacian; low = blurred
    phash       TEXT,
    indexed_at  TEXT
);

CREATE TABLE IF NOT EXISTS embeddings (
    sha256 TEXT NOT NULL,
    model  TEXT NOT NULL,
    dim    INTEGER NOT NULL,
    vec    BLOB NOT NULL,
    PRIMARY KEY (sha256, model)
);

CREATE TABLE IF NOT EXISTS faces (
    face_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256     TEXT NOT NULL,
    x          REAL, y REAL, w REAL, h REAL,
    score      REAL,
    rotation   INTEGER,
    engine     TEXT NOT NULL,
    dim        INTEGER,
    vec        BLOB,
    cluster_id INTEGER,
    person_id  INTEGER
);
CREATE INDEX IF NOT EXISTS faces_sha    ON faces(sha256);
CREATE INDEX IF NOT EXISTS faces_person ON faces(person_id);

CREATE TABLE IF NOT EXISTS people (
    person_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name      TEXT,
    locked    INTEGER NOT NULL DEFAULT 0,  -- hand-named survives re-clustering
    dim       INTEGER,
    centroid  BLOB,
    n_faces   INTEGER DEFAULT 0
);

-- Provenance is not optional: an inferred date must never look like an EXIF fact.
CREATE TABLE IF NOT EXISTS dates (
    sha256     TEXT PRIMARY KEY,
    value      TEXT,
    source     TEXT NOT NULL,
    confidence REAL NOT NULL,
    detail     TEXT
);

CREATE TABLE IF NOT EXISTS geo (
    sha256     TEXT PRIMARY KEY,
    lat        REAL, lon REAL,
    place      TEXT, admin1 TEXT, country TEXT,
    source     TEXT NOT NULL,
    confidence REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS classes (
    sha256 TEXT NOT NULL,
    class  TEXT NOT NULL,
    score  REAL NOT NULL,
    -- Where the label came from: 'clip' for the zero-shot guess, 'rule:<name>'
    -- when file metadata settled it outright. Same reason dates carry a source —
    -- a measured fact and a guess must never be indistinguishable.
    source TEXT NOT NULL DEFAULT 'clip',
    PRIMARY KEY (sha256, class)
);

-- What a photograph is OF. Multi-label by design: dinner on a terrace at sunset
-- is food AND sunset AND architecture, unlike `classes` where a file is exactly
-- one thing.
CREATE TABLE IF NOT EXISTS subjects (
    sha256  TEXT NOT NULL,
    grp     TEXT NOT NULL,
    subject TEXT NOT NULL,
    score   REAL NOT NULL,
    PRIMARY KEY (sha256, subject)
);
CREATE INDEX IF NOT EXISTS subjects_subject ON subjects(subject);

-- Which occasion a photograph belongs to. `source` distinguishes a name the
-- operator curated by hand from one this package inferred from timestamps —
-- the same provenance rule dates, places and classes already follow.
CREATE TABLE IF NOT EXISTS events (
    sha256     TEXT PRIMARY KEY,
    year       TEXT NOT NULL,
    event_key  TEXT NOT NULL,
    event_name TEXT NOT NULL,
    source     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_key ON events(year, event_key);

CREATE TABLE IF NOT EXISTS captions (
    sha256 TEXT NOT NULL,
    text   TEXT NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (sha256, text, source)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


# ── connection ──────────────────────────────────────────────────────────────
def db_path(root: Path) -> Path:
    return Path(root).resolve() / ".mediaexplorer" / DB_NAME


def connect(root: Path, *, create: bool = True) -> sqlite3.Connection:
    """Open (and migrate) the catalog for ``root``."""
    p = db_path(root)
    if not create and not p.exists():
        raise FileNotFoundError(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_SCHEMA)
    _migrate(conn)
    cur = conn.execute("SELECT value FROM meta WHERE key='schema_version'")
    row = cur.fetchone()
    if row is None:
        conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                     (str(SCHEMA_VERSION),))
    else:
        conn.execute("UPDATE meta SET value=? WHERE key='schema_version'",
                     (str(SCHEMA_VERSION),))
    conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive migrations for catalogs created by an earlier version.

    `CREATE TABLE IF NOT EXISTS` silently does nothing when the table already
    exists, so a new column never appears on a real 92k-row catalog without this.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(classes)")}
    if cols and "source" not in cols:
        conn.execute("ALTER TABLE classes ADD COLUMN source TEXT NOT NULL DEFAULT 'clip'")
        conn.commit()


def get_meta(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, str(value)))
    conn.commit()


# ── ingest ──────────────────────────────────────────────────────────────────
def _norm(p: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(p)))


def ingest_meta_jsonl(conn: sqlite3.Connection, root: Path, *,
                      quiet: bool = False) -> dict[str, int]:
    """Fold ``.mediaexplorer/meta.jsonl`` (written by `explore probe`) into ``files``.

    Idempotent: re-running updates rows in place and never duplicates. Rows whose
    size+mtime are unchanged keep their ``sha256``, so a re-ingest never forces a
    re-hash of the whole library.
    """
    root = Path(root).resolve()
    src = root / ".mediaexplorer" / "meta.jsonl"
    stats = {"seen": 0, "inserted": 0, "updated": 0, "skipped": 0}
    if not src.exists():
        return stats

    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    existing: dict[str, tuple[int, float]] = {
        r["path"]: (r["size"], r["mtime"])
        for r in conn.execute("SELECT path, size, mtime FROM files")
    }
    batch: list[tuple] = []

    with src.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001 - a truncated sidecar line is not fatal
                continue
            if r.get("type") not in (None, "image"):
                # videos/audio are probed into the same sidecar; vision is images-only
                continue
            abs_p = r.get("abs") or str(root / r.get("rel", ""))
            path = _norm(abs_p)
            stats["seen"] += 1
            gps = r.get("gps") or [None, None]
            batch.append((
                path, str(root), r.get("rel", ""), r.get("name", ""), r.get("ext", ""),
                int(r.get("size") or 0), float(r.get("mtime") or 0.0), now,
                1 if r.get("probe_ok") else 0,
                int(r.get("w") or 0), int(r.get("h") or 0),
                r.get("camera") or "", r.get("created") or "",
                gps[0], gps[1],
                r.get("res_tier") or "", r.get("source_class") or "",
            ))
            if path in existing:
                stats["updated"] += 1
            else:
                stats["inserted"] += 1

    # Preserve sha256 across re-ingest when the bytes cannot have changed.
    conn.executemany(
        """
        INSERT INTO files (path, root, rel, name, ext, size, mtime, first_seen,
                           p_ok, p_w, p_h, p_camera, p_created,
                           p_gps_lat, p_gps_lon, p_res_tier, p_source_class, present)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
        ON CONFLICT(path) DO UPDATE SET
            root=excluded.root, rel=excluded.rel, name=excluded.name, ext=excluded.ext,
            size=excluded.size, mtime=excluded.mtime,
            -- `present` is owned by the pipeline, which looks at the disk, NOT by
            -- the sidecar, which is an append-only history and may be stale.
            -- Resetting it to 1 here un-flagged 22,669 files the decode pass had
            -- just found missing, so every run re-walked the same phantoms.
            -- Only a genuine re-observation (changed size/mtime) revives a row.
            present = CASE
                WHEN files.size = excluded.size AND files.mtime = excluded.mtime
                THEN files.present ELSE 1 END,
            p_ok=excluded.p_ok, p_w=excluded.p_w, p_h=excluded.p_h,
            p_camera=excluded.p_camera, p_created=excluded.p_created,
            p_gps_lat=excluded.p_gps_lat, p_gps_lon=excluded.p_gps_lon,
            p_res_tier=excluded.p_res_tier, p_source_class=excluded.p_source_class,
            sha256 = CASE
                WHEN files.size = excluded.size AND files.mtime = excluded.mtime
                THEN files.sha256 ELSE NULL END
        """,
        batch,
    )
    conn.commit()
    if not quiet:
        print(f"[catalog] meta.jsonl → {stats['seen']} image rows "
              f"({stats['inserted']} new, {stats['updated']} refreshed)", flush=True)
    return stats


def mark_missing(conn: sqlite3.Connection, root: Path) -> int:
    """Flag rows whose file no longer exists. Never deletes — history is evidence."""
    gone = []
    for r in conn.execute("SELECT path FROM files WHERE present=1 AND root=?", (str(Path(root).resolve()),)):
        if not os.path.exists(r["path"]):
            gone.append((r["path"],))
    if gone:
        conn.executemany("UPDATE files SET present=0 WHERE path=?", gone)
        conn.commit()
    return len(gone)


# ── vectors ─────────────────────────────────────────────────────────────────
def pack_vec(vec) -> bytes:
    """L2-normalise and store as fp16 — half the bytes, no measurable recall loss."""
    import numpy as np  # noqa: PLC0415

    a = np.asarray(vec, dtype=np.float32).ravel()
    n = float(np.linalg.norm(a))
    if n > 0:
        a = a / n
    return a.astype(np.float16).tobytes()


def unpack_vec(blob: bytes):
    import numpy as np  # noqa: PLC0415

    return np.frombuffer(blob, dtype=np.float16).astype(np.float32)


def load_matrix(conn: sqlite3.Connection, model: str):
    """Return ``(keys, matrix)`` — an (N, dim) float32 array of unit vectors.

    Brute force over this is exact and takes single-digit milliseconds at 100k
    rows, which is why there is no ANN index anywhere in this package.
    """
    import numpy as np  # noqa: PLC0415

    keys: list[str] = []
    rows: list[bytes] = []
    for r in conn.execute("SELECT sha256, vec FROM embeddings WHERE model=? ORDER BY sha256",
                          (model,)):
        keys.append(r["sha256"])
        rows.append(r["vec"])
    if not rows:
        return [], np.zeros((0, 0), dtype=np.float32)
    mat = np.frombuffer(b"".join(rows), dtype=np.float16).astype(np.float32)
    return keys, mat.reshape(len(rows), -1)


# ── work queues ─────────────────────────────────────────────────────────────
def iter_pending(conn: sqlite3.Connection, root: Path, *,
                 limit: int | None = None) -> Iterator[sqlite3.Row]:
    """Files that still need the decode pass — resumable by construction.

    A file is pending when it has no sha256 yet, or its sha256 has no ``assets``
    row. Interrupting a 90-minute library run therefore costs only the batch in
    flight, which is the failure that bit the earlier dedup work.
    """
    sql = """
        SELECT f.path, f.rel, f.size, f.mtime, f.sha256
        FROM files f
        LEFT JOIN assets a ON a.sha256 = f.sha256
        WHERE f.present = 1 AND f.root = ?
          AND (f.sha256 IS NULL OR a.sha256 IS NULL)
        ORDER BY f.size
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    yield from conn.execute(sql, (str(Path(root).resolve()),))


def counts(conn: sqlite3.Connection, root: Path | None = None) -> dict[str, int]:
    where, args = "", ()
    if root is not None:
        where, args = " WHERE root = ?", (str(Path(root).resolve()),)
    q = lambda sql, a=(): conn.execute(sql, a).fetchone()[0]  # noqa: E731
    return {
        "files": q(f"SELECT COUNT(*) FROM files{where}", args),
        "hashed": q(f"SELECT COUNT(*) FROM files{where}{' AND' if where else ' WHERE'} sha256 IS NOT NULL", args),
        "assets": q("SELECT COUNT(*) FROM assets"),
        "decoded": q("SELECT COUNT(*) FROM assets WHERE decoded=1"),
        "undecodable": q("SELECT COUNT(*) FROM assets WHERE decoded=0"),
        "embeddings": q("SELECT COUNT(*) FROM embeddings"),
        "faces": q("SELECT COUNT(*) FROM faces"),
        "people": q("SELECT COUNT(*) FROM people"),
        "dated": q("SELECT COUNT(*) FROM dates"),
        "geocoded": q("SELECT COUNT(*) FROM geo"),
        "classified": q("SELECT COUNT(DISTINCT sha256) FROM classes"),
    }
