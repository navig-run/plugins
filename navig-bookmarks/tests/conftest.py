"""Shared fixtures.

navig, navig-sdk and (once installed) this plugin are editable-installed against the MAIN
checkout. Put THIS tree's copies first so a worktree tests the code under change.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[1]
_REPO = _PLUGIN.parents[1]
for _root in (_REPO / "core", _REPO / "registry" / "sdk" / "python", _PLUGIN):
    if _root.is_dir() and str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    """A throwaway bookmarks.db, and no reach into the real navig data dir."""
    path = tmp_path / "bookmarks.db"
    monkeypatch.setenv("NAVIG_BOOKMARKS_DB", str(path))
    monkeypatch.setenv("NAVIG_BOOKMARKS_NO_MIGRATE", "1")
    from navig_bookmarks import migrate
    from navig_bookmarks.commands import bookmarks as cmd

    monkeypatch.setattr(migrate, "legacy_path", lambda: tmp_path / "no-links.db")
    cmd._STORES.clear()
    yield path
    for store in cmd._STORES.values():
        store.close()
    cmd._STORES.clear()


@pytest.fixture()
def store(db_path):
    from navig_bookmarks.store import BookmarkStore

    s = BookmarkStore(db_path)
    yield s
    s.close()


@pytest.fixture()
def netscape_fixture() -> str:
    return (FIXTURES / "netscape.html").read_text(encoding="utf-8")
