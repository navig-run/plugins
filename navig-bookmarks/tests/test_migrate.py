"""links.db -> bookmarks.db: read-only on the source, idempotent, never resurrects."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from typer.testing import CliRunner

from navig_bookmarks.migrate import migrate_legacy_links

# The schema navig core's links store writes (core/navig/memory/links_db.py).
_LEGACY_SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    id TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT, notes TEXT, tags TEXT DEFAULT '[]',
    vault_cred_id TEXT, last_visited DATETIME, visit_count INTEGER DEFAULT 0,
    screenshot_path TEXT, favicon_path TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""


def _legacy(path, rows, *, wal=False):
    con = sqlite3.connect(str(path), timeout=5)
    if wal:
        con.execute("PRAGMA journal_mode=WAL")
    con.executescript(_LEGACY_SCHEMA)
    for r in rows:
        con.execute(
            "INSERT INTO links (id, url, title, notes, tags, vault_cred_id, visit_count, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (r["id"], r["url"], r.get("title"), r.get("notes"), json.dumps(r.get("tags", [])),
             r.get("cred"), r.get("visits", 0), "2025-01-02 03:04:05"),
        )
    con.commit()
    con.close()


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


ROWS = [
    {"id": "aaaa1111", "url": "https://github.com", "title": "GitHub", "tags": ["dev", "work"], "cred": "c1",
     "visits": 4},
    {"id": "bbbb2222", "url": "https://example.com/x", "notes": "Imported folder: bookmark_bar/Dev/Tools",
     "tags": ["imported", "chrome"]},
    {"id": "cccc3333", "url": "https://example.org", "notes": "keep this note"},
]


def test_copies_rows_with_ids_tags_and_folders(store, tmp_path) -> None:
    src = tmp_path / "links.db"
    _legacy(src, ROWS)
    res = migrate_legacy_links(store, src)
    assert (res.status, res.copied) == ("copied", 3)
    gh = store.get("aaaa1111")
    assert gh.title == "GitHub" and gh.tags == {"dev": "user", "work": "user"}
    assert gh.vault_cred_id == "c1" and gh.visit_count == 4 and gh.created_at == "2025-01-02 03:04:05"
    x = store.get("bbbb2222")
    assert x.folder_path == "bookmark_bar/Dev/Tools" and x.notes is None
    assert store.get("cccc3333").notes == "keep this note"
    assert [b.id for b in store.search("github")] == ["aaaa1111"]


def test_running_twice_is_a_noop_and_the_source_is_untouched(store, tmp_path) -> None:
    src = tmp_path / "links.db"
    _legacy(src, ROWS)
    before = (_digest(src), src.stat().st_mtime_ns)
    first = migrate_legacy_links(store, src)
    second = migrate_legacy_links(store, src)
    assert first.copied == 3
    assert second.status == "unchanged" and second.copied == 0
    assert store.count() == 3
    assert (_digest(src), src.stat().st_mtime_ns) == before


def test_new_legacy_rows_are_picked_up_but_deleted_ones_stay_deleted(store, tmp_path) -> None:
    src = tmp_path / "links.db"
    _legacy(src, ROWS)
    migrate_legacy_links(store, src)
    store.delete("cccc3333")
    _legacy(src, [{"id": "dddd4444", "url": "https://new.example"}])  # core still writes links.db
    res = migrate_legacy_links(store, src)
    assert res.copied == 1 and res.already_migrated == 3
    assert store.get("dddd4444") is not None
    assert store.get("cccc3333") is None  # not resurrected


def test_a_url_already_saved_is_not_duplicated(store, tmp_path) -> None:
    store.add("http://www.github.com/", title="mine")
    src = tmp_path / "links.db"
    _legacy(src, ROWS[:1])
    res = migrate_legacy_links(store, src)
    assert res.copied == 0 and res.skipped_duplicates == 1 and store.count() == 1


def test_wal_source_is_read(store, tmp_path) -> None:
    src = tmp_path / "links.db"
    _legacy(src, ROWS, wal=True)
    assert migrate_legacy_links(store, src).copied == 3


def test_absent_source(store, tmp_path) -> None:
    assert migrate_legacy_links(store, tmp_path / "missing.db").status == "absent"


def test_first_cli_use_migrates_automatically(tmp_path, monkeypatch) -> None:
    from navig_bookmarks import migrate
    from navig_bookmarks.commands import bookmarks as cmd

    src = tmp_path / "links.db"
    _legacy(src, ROWS)
    monkeypatch.setenv("NAVIG_BOOKMARKS_DB", str(tmp_path / "bookmarks.db"))
    monkeypatch.delenv("NAVIG_BOOKMARKS_NO_MIGRATE", raising=False)
    monkeypatch.setattr(migrate, "legacy_path", lambda: src)
    cmd._STORES.clear()
    try:
        result = CliRunner().invoke(cmd.app, ["list", "--json"])
        assert result.exit_code == 0, result.output
        assert {row["id"] for row in json.loads(result.stdout)} == {"aaaa1111", "bbbb2222", "cccc3333"}
    finally:
        for s in cmd._STORES.values():
            s.close()
        cmd._STORES.clear()


def test_garbage_source_raises_a_database_error(store, tmp_path) -> None:
    import pytest

    src = tmp_path / "links.db"
    src.write_text("not a database at all, just text" * 20, encoding="utf-8")
    with pytest.raises(sqlite3.DatabaseError):
        migrate_legacy_links(store, src)
