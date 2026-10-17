"""The cabinet: an encrypted catalog plus one encrypted object file per item.

On disk (``<config_dir>/cabinet/``, or ``NAVIG_CABINET_DIR``)::

    key.json            the wrapped master key (see keys.py)
    cabinet.db          SQLite catalog — one row per item
    objects/<id>.bin    the file itself, stream-encrypted (see stream.py)
    .open/              short-lived decrypted copies made by `navig cabinet open`

What is readable without the key is only what the row layout forces: an id, two
timestamps and an active/trashed flag. The title, original filename, category, tags,
expiry date, issuer, notes and every word of OCR text live inside ``meta``, sealed with
the item's own data key. A stolen laptop disk shows that the cabinet holds N items of
roughly these sizes — not that one of them is a psychiatric report.

Search therefore decrypts the whole catalog into memory and matches there. At the
scale a person's documents reach (thousands of items, a few hundred KB of text each at
most) that is milliseconds; a plaintext full-text index would be faster at a scale
nobody's filing cabinet reaches, and would put the medical vocabulary back on disk.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import BinaryIO

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import keys
from .stream import StreamDecryptor, StreamEncryptor, StreamError

from navig_sdk.host import command_name  # noqa: E402

# The command a user types here: `navig cabinet` inside navig, `navig-cabinet` on its own.
CMD = command_name("cabinet", standalone="navig-cabinet")

DB_FILE = "cabinet.db"
OBJECTS = "objects"
OPEN_DIR = ".open"
OPEN_TTL_SECONDS = 15 * 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'active',
    wrapped_dek BLOB NOT NULL,
    meta        BLOB NOT NULL
);
"""


class CabinetError(Exception):
    """A cabinet operation failed in a way the operator must hear about."""


class NotFound(CabinetError):
    pass


class Ambiguous(CabinetError):
    pass


class Duplicate(CabinetError):
    def __init__(self, existing: Item):
        super().__init__(f"already in the cabinet as {existing.id} ({existing.title})")
        self.existing = existing


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class Item:
    id: str
    title: str
    original_name: str
    kind: str
    mime: str
    category: str
    size: int
    sha256: str
    added_at: str
    updated_at: str = ""
    tags: list[str] = field(default_factory=list)
    expires: str | None = None     # YYYY-MM-DD
    expires_detected: bool = False  # True when read from the document, not typed
    issuer: str | None = None
    notes: str | None = None
    text: str = ""
    text_source: str = ""
    state: str = "active"

    def public(self, *, with_text: bool = False) -> dict:
        d = asdict(self)
        if not with_text:
            d.pop("text")
        return d

    def days_to_expiry(self, today: date | None = None) -> int | None:
        if not self.expires:
            return None
        try:
            exp = date.fromisoformat(self.expires)
        except ValueError:
            return None
        return (exp - (today or date.today())).days


_META_FIELDS = ("title", "original_name", "kind", "mime", "category", "size", "sha256",
                "added_at", "tags", "expires", "expires_detected", "issuer", "notes", "text",
                "text_source")


def default_root() -> Path:
    env = os.environ.get("NAVIG_CABINET_DIR")
    if env:
        return Path(env)
    from navig_vault._compat import config_dir

    return config_dir() / "cabinet"


class Cabinet:
    """An unlocked cabinet. Construct with :meth:`open`, or :meth:`create` the first time."""

    def __init__(self, root: Path, master: bytes):
        self.root = root
        self._master = master
        self._aead = AESGCM(master)
        (root / OBJECTS).mkdir(parents=True, exist_ok=True)
        self._sha_index: dict[str, str] | None = None
        # Set by every change to what is stored; close() then refreshes the reminder
        # index once — not per change, so a bulk add of N files stays linear.
        self._dirty = False
        self._db_path = root / DB_FILE
        fresh = not self._db_path.exists()
        self._db = sqlite3.connect(self._db_path, timeout=5.0)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript(_SCHEMA)
        self._db.commit()
        if fresh:
            keys._owner_only(self._db_path)
        self.sweep_open_copies()

    # ── lifecycle ───────────────────────────────────────────────────────────
    @classmethod
    def create(cls, root: Path) -> Cabinet:
        return cls(root, keys.create(root))

    @classmethod
    def open(cls, root: Path, passphrase: str | None = None) -> Cabinet:
        return cls(root, keys.KeyFile.load(root).unwrap(passphrase))

    @staticmethod
    def exists(root: Path) -> bool:
        return keys.KeyFile.exists(root)

    def close(self) -> None:
        if self._dirty:
            from .reminders import write_index

            write_index(self.root, self.items())
            self._dirty = False
        self._db.close()

    def __enter__(self) -> Cabinet:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def master_key(self) -> bytes:
        return self._master

    # ── per-item crypto ─────────────────────────────────────────────────────
    def _wrap_dek(self, item_id: str, dek: bytes) -> bytes:
        nonce = os.urandom(12)
        return nonce + self._aead.encrypt(nonce, dek, b"dek/" + item_id.encode())

    def _unwrap_dek(self, item_id: str, blob: bytes) -> bytes:
        try:
            return self._aead.decrypt(blob[:12], blob[12:], b"dek/" + item_id.encode())
        except Exception as exc:  # noqa: BLE001
            raise CabinetError(f"item {item_id}: its key does not decrypt (catalog tampered?)") from exc

    @staticmethod
    def _seal_meta(dek: bytes, item_id: str, meta: dict) -> bytes:
        nonce = os.urandom(12)
        body = json.dumps(meta, ensure_ascii=False).encode("utf-8")
        return nonce + AESGCM(dek).encrypt(nonce, body, b"meta/" + item_id.encode())

    @staticmethod
    def _open_meta(dek: bytes, item_id: str, blob: bytes) -> dict:
        try:
            body = AESGCM(dek).decrypt(blob[:12], blob[12:], b"meta/" + item_id.encode())
        except Exception as exc:  # noqa: BLE001
            raise CabinetError(f"item {item_id}: metadata does not decrypt (tampered?)") from exc
        return json.loads(body.decode("utf-8"))

    def object_path(self, item_id: str) -> Path:
        return self.root / OBJECTS / f"{item_id}.bin"

    # ── reading the catalog ─────────────────────────────────────────────────
    def _rows(self, include_trashed: bool) -> Iterator[tuple]:
        sql = "SELECT id, created_at, updated_at, state, wrapped_dek, meta FROM items"
        if not include_trashed:
            sql += " WHERE state = 'active'"
        yield from self._db.execute(sql + " ORDER BY created_at, id")

    def _item_from_row(self, row: tuple) -> tuple[Item, bytes]:
        item_id, _created, updated, state, wrapped, meta_blob = row
        dek = self._unwrap_dek(item_id, wrapped)
        meta = self._open_meta(dek, item_id, meta_blob)
        known = {k: meta[k] for k in _META_FIELDS if k in meta}
        return Item(id=item_id, updated_at=updated, state=state, **known), dek

    def items(self, *, include_trashed: bool = False) -> list[Item]:
        return [self._item_from_row(r)[0] for r in self._rows(include_trashed)]

    def _get_with_dek(self, item_id: str) -> tuple[Item, bytes]:
        row = self._db.execute(
            "SELECT id, created_at, updated_at, state, wrapped_dek, meta FROM items WHERE id = ?",
            (item_id,),
        ).fetchone()
        if row is None:
            raise NotFound(f"no item {item_id}")
        return self._item_from_row(row)

    def get(self, item_id: str) -> Item:
        return self._get_with_dek(item_id)[0]

    def resolve(self, ref: str, *, include_trashed: bool = False) -> Item:
        """An exact id, or an unambiguous id prefix of at least 3 characters."""
        ref = ref.strip().lower()
        row = self._db.execute("SELECT id FROM items WHERE id = ?", (ref,)).fetchone()
        if row:
            item = self.get(row[0])
            if item.state == "trashed" and not include_trashed:
                raise NotFound(f"{ref} is in the trash — `{CMD} undelete {ref}`")
            return item
        if len(ref) < 3:
            raise NotFound(f"no item {ref!r}")
        states = ("active", "trashed") if include_trashed else ("active",)
        hits = [r[0] for r in self._db.execute(
            f"SELECT id FROM items WHERE id LIKE ? AND state IN ({','.join('?' * len(states))})",
            (ref + "%", *states),
        )]
        if not hits:
            raise NotFound(f"no item {ref!r}")
        if len(hits) > 1:
            raise Ambiguous(f"{ref!r} matches {len(hits)} items: {', '.join(sorted(hits)[:6])}")
        return self.get(hits[0])

    def find_by_sha(self, sha256: str) -> Item | None:
        # Built once per open cabinet and kept current by add/purge: a bulk `add` of N
        # files would otherwise decrypt the whole catalog N times.
        if self._sha_index is None:
            self._sha_index = {it.sha256: it.id for it in self.items(include_trashed=True)}
        item_id = self._sha_index.get(sha256)
        return self.get(item_id) if item_id else None

    # ── writing ─────────────────────────────────────────────────────────────
    def _new_id(self) -> str:
        while True:
            candidate = secrets.token_hex(4)
            if not self._db.execute("SELECT 1 FROM items WHERE id = ?", (candidate,)).fetchone():
                return candidate

    def add_stream(self, src: BinaryIO, *, original_name: str, kind: str, mime: str,
                   category: str, title: str | None = None, tags: list[str] | None = None,
                   expires: str | None = None, issuer: str | None = None,
                   notes: str | None = None, text: str = "", text_source: str = "",
                   expires_detected: bool = False,
                   expected_sha256: str | None = None, allow_duplicate: bool = False,
                   added_at: str | None = None) -> Item:
        """Encrypt *src* into the cabinet, verify it by reading it back, then catalog it.

        The object is written to a temp name and only renamed into place after a full
        decrypt-and-hash read-back matches, so a crash, a full disk or a bad sector can
        leave a stray temp file but never a catalog row pointing at a broken object.
        """
        item_id = self._new_id()
        dek = os.urandom(32)
        final = self.object_path(item_id)
        tmp = final.with_suffix(".bin.tmp")
        try:
            with tmp.open("wb") as out:
                enc = StreamEncryptor(out, dek, item_id.encode())
                shutil.copyfileobj(src, enc, 1024 * 1024)
                enc.close()
                out.flush()
                os.fsync(out.fileno())
            size, sha = enc.size, enc.sha256
            if expected_sha256 and sha != expected_sha256:
                raise CabinetError(
                    f"{original_name}: content does not match the expected checksum — not stored"
                )
            if not allow_duplicate:
                existing = self.find_by_sha(sha)
                if existing is not None:
                    raise Duplicate(existing)
            with tmp.open("rb") as check:
                back_size, back_sha = _verify_stream(check, dek, item_id.encode())
            if (back_size, back_sha) != (size, sha):
                raise CabinetError(f"{original_name}: read-back verification failed — not stored")
            os.replace(tmp, final)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

        now = _now()
        meta = {
            "title": title or Path(original_name).stem, "original_name": original_name,
            "kind": kind, "mime": mime, "category": category, "size": size, "sha256": sha,
            "added_at": added_at or now, "tags": tags or [], "expires": expires,
            "expires_detected": bool(expires and expires_detected), "issuer": issuer, "notes": notes, "text": text, "text_source": text_source,
        }
        try:
            with self._db:
                self._db.execute(
                    "INSERT INTO items (id, created_at, updated_at, state, wrapped_dek, meta) "
                    "VALUES (?, ?, ?, 'active', ?, ?)",
                    (item_id, now, now, self._wrap_dek(item_id, dek), self._seal_meta(dek, item_id, meta)),
                )
        except BaseException:
            final.unlink(missing_ok=True)
            raise
        if self._sha_index is not None:
            self._sha_index[sha] = item_id
        self._dirty = True
        return self.get(item_id)

    def update(self, item_id: str, **changes) -> Item:
        item, dek = self._get_with_dek(item_id)
        meta = {k: getattr(item, k) for k in _META_FIELDS}
        for key, value in changes.items():
            if key not in {"title", "category", "tags", "expires", "expires_detected", "issuer", "notes"}:
                raise ValueError(f"cannot edit {key!r}")
            meta[key] = value
        with self._db:
            self._db.execute(
                "UPDATE items SET meta = ?, updated_at = ? WHERE id = ?",
                (self._seal_meta(dek, item_id, meta), _now(), item_id),
            )
        self._dirty = True
        return self.get(item_id)

    def set_state(self, item_id: str, state: str) -> None:
        with self._db:
            self._db.execute("UPDATE items SET state = ?, updated_at = ? WHERE id = ?",
                             (state, _now(), item_id))
        self._dirty = True

    def purge(self, item_id: str) -> None:
        self._sha_index = None
        self._dirty = True
        with self._db:
            self._db.execute("DELETE FROM items WHERE id = ?", (item_id,))
        self.object_path(item_id).unlink(missing_ok=True)

    # ── reading contents ────────────────────────────────────────────────────
    def open_stream(self, item_id: str) -> StreamDecryptor:
        """A verifying reader over an item's plaintext. The caller closes the file."""
        _, dek = self._get_with_dek(item_id)
        path = self.object_path(item_id)
        if not path.is_file():
            raise CabinetError(f"item {item_id}: its encrypted file is missing ({path.name})")
        return StreamDecryptor(path.open("rb"), dek, item_id.encode())

    def write_plaintext(self, item: Item, dest: Path) -> Path:
        """Decrypt *item* to *dest* (atomic, verified against the recorded checksum)."""
        tmp = dest.with_name(dest.name + ".part")
        dec = self.open_stream(item.id)
        try:
            with tmp.open("wb") as out:
                dec.read_to_end(out)
            if dec.sha256 != item.sha256:
                raise CabinetError(f"{item.id}: decrypted content does not match its checksum")
            os.replace(tmp, dest)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        finally:
            dec.close()
        return dest

    def verify(self, item: Item) -> bool:
        dec = self.open_stream(item.id)
        try:
            dec.read_to_end()
        except StreamError:
            return False
        finally:
            dec.close()
        return dec.sha256 == item.sha256

    def export(self, item: Item, out_dir: Path, *, subdir: str | None = None) -> Path:
        target_dir = out_dir / subdir if subdir else out_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        return self.write_plaintext(item, unique_path(target_dir, safe_filename(item.original_name)))

    # ── `open`: short-lived decrypted copies ────────────────────────────────
    def open_copy(self, item: Item) -> Path:
        d = self.root / OPEN_DIR
        d.mkdir(parents=True, exist_ok=True)
        return self.write_plaintext(item, unique_path(d, safe_filename(item.original_name)))

    def sweep_open_copies(self, *, max_age: float = OPEN_TTL_SECONDS) -> int:
        d = self.root / OPEN_DIR
        if not d.is_dir():
            return 0
        removed = 0
        cutoff = time.time() - max_age
        for p in d.iterdir():
            try:
                if max_age <= 0 or p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except OSError:
                pass  # still open in a viewer on Windows — the next sweep gets it
        return removed

    def open_copies(self) -> list[Path]:
        d = self.root / OPEN_DIR
        return sorted(d.iterdir()) if d.is_dir() else []


def _verify_stream(src: BinaryIO, key: bytes, context: bytes) -> tuple[int, str]:
    dec = StreamDecryptor(src, key, context)
    dec.read_to_end()
    return dec.size, dec.sha256


_BAD_CHARS = '<>:"/\\|?*' + "".join(chr(c) for c in range(32))


def safe_filename(name: str) -> str:
    """The original name, minus what the filesystem rejects. Never a path."""
    base = Path(name.replace("\\", "/")).name or "item"
    cleaned = "".join("_" if c in _BAD_CHARS else c for c in base).strip(" .")
    return cleaned or "item"


def unique_path(directory: Path, name: str) -> Path:
    """``name``, or ``name (2)``, ``name (3)``… — never an existing file."""
    candidate = directory / name
    stem, suffix = Path(name).stem, Path(name).suffix
    n = 2
    while candidate.exists() or candidate.with_name(candidate.name + ".part").exists():
        candidate = directory / f"{stem} ({n}){suffix}"
        n += 1
    return candidate


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


__all__ = ["Cabinet", "CabinetError", "NotFound", "Ambiguous", "Duplicate", "Item",
           "default_root", "safe_filename", "unique_path", "sha256_file"]
