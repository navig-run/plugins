"""The scheduler's default dir was debranded `~/.config/the engine` → `~/.config/navig-github`
(in step with ConfigManager). Schedules saved under the old path must be adopted once, so an
upgrade doesn't silently lose the user's scheduled backups. (The wipe/atomic behaviour itself
is covered by test_scheduler_no_wipe.py.)
"""

from __future__ import annotations

import json

from navig_github.engine.scheduler import BackupScheduler


def test_legacy_schedules_are_migrated(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "schedules.json").write_text(
        json.dumps({"schedules": {"nightly": {
            "name": "nightly", "profile_name": "p", "interval": "daily",
        }}}),
        encoding="utf-8",
    )
    new = tmp_path / "new"
    monkeypatch.setattr(BackupScheduler, "DEFAULT_SCHEDULE_DIR", new)
    monkeypatch.setattr(BackupScheduler, "LEGACY_SCHEDULE_DIR", legacy)

    sched = BackupScheduler()  # no dir arg → default dir → one-time migration runs
    assert sched.get_backup("nightly") is not None
    assert (legacy / "schedules.json").exists()  # non-destructive: legacy left in place


def test_no_migration_when_explicit_dir(tmp_path, monkeypatch):
    # an explicit schedule_dir must NOT trigger adoption from the legacy default
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "schedules.json").write_text(
        json.dumps({"schedules": {"x": {"name": "x", "profile_name": "p", "interval": "daily"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(BackupScheduler, "LEGACY_SCHEDULE_DIR", legacy)
    sched = BackupScheduler(schedule_dir=tmp_path / "explicit")
    assert sched.get_backup("x") is None
