"""The contacts engine: vCard parsing, identifier normalisation, record
linkage and photo extraction.

The SQLite schema lives in :mod:`navig_contacts.store` (``ContactStore``), which navig's
message dispatch reads too, as ``navig.store.contacts``; this package only decides which book
to open and what to put in it.
"""

from .db import base_dir, book_path, describe_book, get_db, init_db, use_book

__all__ = [
    "base_dir", "book_path", "describe_book", "get_db", "init_db", "use_book",
]
