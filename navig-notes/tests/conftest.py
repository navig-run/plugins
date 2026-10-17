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


def dock_note(nid: str = "n1", *, rev: int = 1, body: str = "hello", scope: str = "page", updated: int = 1_700_000_000_000,
              **extra) -> dict:
    """A note in the exact shape the Dock (shared/notes-store.js, schema 2) sends."""
    note = {
        "id": nid, "schema": 2, "title": "", "body": body, "scope": scope,
        "pageKey": "github.com/navig-run/labs/issues/12", "siteKey": "github.com",
        "sourceUrl": "https://github.com/navig-run/labs/issues/12", "sourceTitle": "Issue 12",
        "color": "amber", "tags": [], "state": "open", "geom": {"x": 10, "y": 20, "w": 300, "h": 220}, "z": 3,
        "createdAt": 1_700_000_000_000, "updatedAt": updated, "rev": rev,
    }
    note.update(extra)
    return note


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    """A throwaway notes.db, and no reach into the real navig data dir."""
    path = tmp_path / "notes.db"
    monkeypatch.setenv("NAVIG_NOTES_DB", str(path))
    monkeypatch.setenv("NAVIG_DATA_DIR", str(tmp_path / "data"))
    from navig_notes import deck_routes
    from navig_notes.commands import notes as cmd

    cmd._STORES.clear()
    deck_routes._STORE.clear()
    yield path
    for store in [*cmd._STORES.values(), *deck_routes._STORE.values()]:
        store.close()
    cmd._STORES.clear()
    deck_routes._STORE.clear()


@pytest.fixture()
def store(db_path):
    from navig_notes.store import NoteStore

    s = NoteStore(db_path)
    assert s.db_path == db_path  # the resolved path, before the first write
    yield s
    s.close()
