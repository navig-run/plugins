"""Core's `navig links` forwards to this plugin, or says how to install it (exit 1)."""

from __future__ import annotations

import builtins
import importlib
import json

import pytest
from typer.testing import CliRunner

pytest.importorskip("navig")

runner = CliRunner()


@pytest.fixture(autouse=True)
def legacy_data_dir(tmp_path, monkeypatch):
    """Point navig's data dir (and so the legacy links.db) at a temp dir — never the real one."""
    from navig.memory import links_db
    from navig.platform import paths

    data = tmp_path / "navig-data"
    data.mkdir()
    monkeypatch.setattr(paths, "data_dir", lambda: data)
    links_db.reset_links_db()
    yield data
    links_db.reset_links_db()


def _alias_module():
    import navig.commands.links_alias as mod

    return importlib.reload(mod)


def test_links_forwards_to_bookmarks_with_a_deprecation_notice(db_path, legacy_data_dir) -> None:
    mod = _alias_module()
    added = runner.invoke(mod.links_app, ["add", "https://example.com/via-links", "--json"])
    assert added.exit_code == 0, added.output
    assert "deprecated" in added.stderr and "navig bookmarks" in added.stderr
    listed = runner.invoke(mod.links_app, ["list", "--json"])
    assert listed.exit_code == 0
    assert [b["url"] for b in json.loads(listed.stdout)] == ["https://example.com/via-links"]
    # The same command objects: an unknown id is the plugin's usage error, exit 2.
    assert runner.invoke(mod.links_app, ["show", "nope0000"]).exit_code == 2


def test_links_without_the_plugin_prints_the_install_hint_and_exits_1(monkeypatch) -> None:
    real_import = builtins.__import__

    def no_plugin(name, *args, **kwargs):
        if name.startswith("navig_bookmarks"):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_plugin)
    mod = _alias_module()
    for argv in (["list"], ["add", "https://x.example", "--tags", "a"], []):
        result = runner.invoke(mod.links_app, argv)
        assert result.exit_code == 1, (argv, result.output)
        assert "pip install navig-bookmarks" in result.stderr
    monkeypatch.setattr(builtins, "__import__", real_import)
    _alias_module()  # leave the forwarding app in place for later tests


def test_core_mounts_the_alias_and_hides_it_from_the_manifest() -> None:
    from navig.cli.registration import _ALIAS_COMMANDS, _EXTERNAL_CMD_MAP

    assert _EXTERNAL_CMD_MAP["links"] == ("navig.commands.links_alias", "links_app")
    assert "links" in _ALIAS_COMMANDS


def test_links_still_repairs_the_legacy_store_doctor_points_at(db_path, legacy_data_dir) -> None:
    """`navig doctor` names `navig links list` as the repair for links.db's unsafe FTS triggers.

    Forwarding to the plugin alone would make that advice a no-op, so the alias opens the
    legacy store first — and never creates one that does not exist.
    """
    import sqlite3

    from navig.commands.doctor import _external_content_fts_misuse

    mod = _alias_module()
    assert runner.invoke(mod.links_app, ["list", "--json"]).exit_code == 0
    assert not (legacy_data_dir / "links.db").exists()  # nothing invented

    legacy = legacy_data_dir / "links.db"
    con = sqlite3.connect(str(legacy), timeout=5)
    con.executescript(
        """
        CREATE TABLE links (id TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT, notes TEXT,
            tags TEXT DEFAULT '[]', vault_cred_id TEXT, last_visited DATETIME,
            visit_count INTEGER DEFAULT 0, screenshot_path TEXT, favicon_path TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP);
        CREATE VIRTUAL TABLE links_fts USING fts5(id UNINDEXED, url, title, notes, tags,
            content='links', content_rowid='rowid');
        CREATE TRIGGER links_fts_update AFTER UPDATE ON links BEGIN
            UPDATE links_fts SET title = new.title WHERE rowid = old.rowid;
        END;
        INSERT INTO links (id, url, title) VALUES ('old00001', 'https://legacy.example', 'Legacy');
        """
    )
    con.commit()
    assert _external_content_fts_misuse(con)  # the defect doctor reports
    con.close()

    result = runner.invoke(mod.links_app, ["list", "--json"])
    assert result.exit_code == 0, result.output
    con = sqlite3.connect(f"file:{legacy.as_posix()}?mode=ro", uri=True, timeout=5)
    try:
        assert _external_content_fts_misuse(con) == []  # repaired by opening it
    finally:
        con.close()
