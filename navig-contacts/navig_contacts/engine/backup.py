"""Backup helpers — auto-backup before destructive operations."""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import db as db_mod
from .db import get_db


def backup_dir():
    """
    Where backups go, resolved at call time.

    Deliberately not a module constant: tests redirect the database by
    patching the db module, and a constant bound at import time would
    ignore that and copy the real contacts.db into the real backups/
    directory during a test run.
    """
    return db_mod.base_dir() / "backups"


def _ts() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")


def _safe_dest(dest: Path) -> Path:
    """Ensure we never overwrite an existing backup — append _1, _2 … if needed."""
    if not dest.exists():
        return dest
    stem = dest.stem
    suffix = dest.suffix
    parent = dest.parent
    i = 1
    while True:
        candidate = parent / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def _checkpoint() -> None:
    """
    Fold the write-ahead log back into the main database file.

    The connection runs in WAL mode, so recent writes can be sitting in
    contacts.db-wal rather than contacts.db.  A backup that copies only the
    main file would then be silently short — and this backup is the only
    rollback path, since contacts.db is personal data and not in git.
    TRUNCATE both checkpoints and empties the WAL, so the copy is complete.
    """
    try:
        with get_db() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        # A checkpoint can legitimately fail if another connection holds a
        # read lock.  A slightly stale backup beats no backup at all, so
        # carry on rather than aborting the operation being protected.
        pass


def auto_backup(reason: str = "manual") -> Path:
    """
    Copy contacts.db → backups/contacts_YYYYMMDD_HHMMSS_<reason>.db
    Records the backup in backup_log table.
    Returns the backup path.
    """
    dest_dir = backup_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)

    ts = _ts()
    dest_name = f"contacts_{ts}_{reason}.db"
    dest = _safe_dest(dest_dir / dest_name)

    if db_mod.book_path().exists():
        _checkpoint()
        shutil.copy2(db_mod.book_path(), dest)
    else:
        # DB doesn't exist yet — create an empty file so the record is valid
        dest.touch()

    # Record in backup_log
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO backup_log (backup_path, created_at, reason) VALUES (?, ?, ?)",
                (str(dest.relative_to(db_mod.base_dir())), _fmt_now(), reason),
            )
    except Exception:
        pass  # backup_log table may not exist on very first run

    return dest


def _fmt_now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
