"""The plugin's storage paths were debranded `~/.config/the engine` / `.the engine_*`
→ `~/.config/navig-github` / `.navig-github_*`, in step with the profile store. Each
store must still find data written under the old location, so an upgrade doesn't
silently lose the user's notification config / custom templates / diff baseline.
"""

from __future__ import annotations

import json


def test_notifications_read_from_legacy_location(tmp_path, monkeypatch):
    from navig_github.engine.notifications import NotificationManager

    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    new_dir = tmp_path / "new"
    (legacy_dir / ".the engine_notifications.json").write_text(
        json.dumps({"email_enabled": True, "email_from": "x@example.com"}), encoding="utf-8"
    )
    monkeypatch.setattr(NotificationManager, "DEFAULT_CONFIG_DIR", new_dir)
    monkeypatch.setattr(NotificationManager, "LEGACY_CONFIG_DIR", legacy_dir)

    mgr = NotificationManager()  # default dir, new file absent → reads legacy
    assert mgr.config.email_enabled is True


def test_templates_read_from_legacy_dir(tmp_path, monkeypatch):
    from navig_github.engine.templates import TemplateManager

    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    new_dir = tmp_path / "new"
    (legacy_dir / "templates.json").write_text(
        json.dumps({"templates": [{"id": "mine", "name": "Mine", "description": "d"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(TemplateManager, "DEFAULT_CONFIG_DIR", new_dir)
    monkeypatch.setattr(TemplateManager, "LEGACY_CONFIG_DIR", legacy_dir)

    mgr = TemplateManager()
    assert any(t.id == "mine" for t in mgr.list_custom())


def test_diff_snapshot_reads_legacy_filename(tmp_path):
    from navig_github.engine.diff import BackupCompare

    snap = {
        "path": str(tmp_path), "created_at": "2026-01-01T00:00:00",
        "repo_count": 0, "total_size_bytes": 0, "repositories": {},
    }
    (tmp_path / ".the engine_snapshot.json").write_text(json.dumps(snap), encoding="utf-8")

    loaded = BackupCompare().load_snapshot(tmp_path)  # new name absent → legacy fallback
    assert loaded is not None and loaded.repo_count == 0
