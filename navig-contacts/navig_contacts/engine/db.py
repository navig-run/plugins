"""Which contact book to open, and making sure it has the schema.

The schema lives in :class:`navig_contacts.store.ContactStore`, which navig reads as
``navig.store.contacts`` because ``dispatch send`` and the Telegram bot use the same tables.
This module only decides *which file* is the book and hands back a connection.

Two books exist, deliberately:

* the **global** one at ``~/.navig/data/contacts.db`` — what ``navig contacts``
  has always meant, and what message dispatch resolves aliases against;
* a **space** one at ``<space>/contacts.db``, reached with ``--space`` — a
  personal address book that belongs to the space it is about, so a client list
  and a private circle are not forced into one table.

The default stays global, so nothing that already works changes.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

#: Set by :func:`use_book`; None means the global store.
_BOOK: Path | None = None

#: The filename a space's contact book has.
BOOK_FILENAME = "contacts.db"


def use_book(path: Path | None) -> None:
    """Pin the book to open (a ``--space`` choice, or a test)."""
    global _BOOK
    _BOOK = Path(path) if path is not None else None


def global_book_path() -> Path:
    """Core's contact book — the one message dispatch reads."""
    from navig_sdk.host import data_dir  # navig's data dir, or the same path standalone

    return data_dir() / BOOK_FILENAME


def resolve_space_dir(space: str) -> Path:
    """The directory of a named space.

    Uses ``discover_space_paths`` rather than ``resolve_space`` so an unknown
    name raises instead of quietly pointing at a directory that does not exist,
    where it would then create an empty book and report zero contacts.
    """
    try:
        from navig.spaces.contracts import normalize_space_name
        from navig.spaces.resolver import discover_space_paths
    except ImportError as exc:  # standalone: there are no navig spaces to look up
        raise ValueError(
            "--space needs navig (spaces are navig's); without it the book is the global one"
        ) from exc

    cfg = (discover_space_paths() or {}).get(normalize_space_name(space))
    root = str(getattr(cfg, "path", "") or "") if cfg is not None else ""
    if not root:
        raise ValueError(f"space not found: {space!r} (see `navig space list`)")
    return Path(root)


def book_path() -> Path:
    """The contact book currently in use."""
    if _BOOK is not None:
        return _BOOK
    return global_book_path()


def base_dir() -> Path:
    """Where this book's backups, photos and reports live: beside the book."""
    return book_path().parent


def init_db() -> None:
    """Create or migrate the schema, through core's store."""
    from navig_contacts.store import ContactStore

    ContactStore(db_path=book_path())


def get_db() -> sqlite3.Connection:
    """A connection to the current book, with the schema guaranteed."""
    init_db()
    conn = sqlite3.connect(book_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # The gateway or a second CLI may hold the write lock; without a timeout the
    # second writer fails instantly rather than waiting the moment it needs.
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def describe_book() -> str:
    """A human phrase for which book is open — used when one turns up empty."""
    path = book_path()
    if _BOOK is None:
        return f"the global contact book ({path})"
    return f"{path.parent.name} ({path})"
