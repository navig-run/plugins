"""The backup scheduler's load-modify-save must never wipe other schedules.

Exists because ``_load_schedules`` used to collapse three distinct states —
"file missing", "file empty", and "file present but transiently unreadable /
corrupt" — into a bare ``{}``. Every mutator (add/remove/enable/disable and the
unattended ``_run_backup``) then did load → modify → save, so a single transient
read failure (a Windows AV/backup sharing violation, a half-written file) turned
into a save of ``{name: ...}`` alone — silently deleting every OTHER scheduled
backup. This is the same "a failed read must not become a destructive write"
class the core config layer guards.
"""

from __future__ import annotations

import json

import pytest

from navig_github.engine.scheduler import (
    BackupScheduler,
    ScheduledBackup,
    ScheduleStoreError,
)


def _mk(name: str) -> ScheduledBackup:
    return ScheduledBackup(name=name, profile_name=f"{name}-profile", interval="daily")


@pytest.fixture
def scheduler(tmp_path):
    return BackupScheduler(schedule_dir=tmp_path)


# ---------------------------------------------------------------------------
# The core guarantee: a transient read failure never wipes the store.
# ---------------------------------------------------------------------------


def test_transient_read_failure_does_not_wipe_other_schedules(scheduler, monkeypatch):
    scheduler.add_backup(_mk("alpha"))
    scheduler.add_backup(_mk("beta"))
    assert {b.name for b in scheduler.list_backups()} == {"alpha", "beta"}

    # Simulate an antivirus/backup agent holding the file for EVERY read attempt.
    def _locked(*_a, **_k):
        raise PermissionError("The process cannot access the file (sharing violation)")

    monkeypatch.setattr(type(scheduler.schedules_path), "read_text", _locked)

    # A mutating caller must REFUSE to save rather than persist an empty store.
    with pytest.raises(ScheduleStoreError):
        scheduler.add_backup(_mk("gamma"))

    # Undo the lock; the on-disk file is intact — nothing was wiped.
    monkeypatch.undo()
    names = {b.name for b in scheduler.list_backups()}
    assert names == {"alpha", "beta"}, f"schedules were wiped: {names}"


def test_run_backup_does_not_crash_or_wipe_on_locked_store(scheduler, monkeypatch):
    """The unattended path persists run-status best-effort — a locked store must
    skip the update, not crash the scheduler thread nor wipe siblings."""
    scheduler.add_backup(_mk("nightly"))
    scheduler.add_backup(_mk("weekly"))
    scheduler.backup_callback = lambda _profile: True

    real_read = type(scheduler.schedules_path).read_text
    calls = {"n": 0}

    def _flaky(self, *a, **k):
        # get_backup (the first read) succeeds; the status-persist read is locked.
        calls["n"] += 1
        if calls["n"] == 1:
            return real_read(self, *a, **k)
        raise PermissionError("locked")

    monkeypatch.setattr(type(scheduler.schedules_path), "read_text", _flaky)
    scheduler._run_backup("nightly")  # must NOT raise
    monkeypatch.undo()

    assert {b.name for b in scheduler.list_backups()} == {"nightly", "weekly"}


# ---------------------------------------------------------------------------
# Corrupt file: preserved, then recoverable (not wedged forever, not wiped-silent).
# ---------------------------------------------------------------------------


def test_corrupt_file_is_quarantined_and_recoverable(scheduler):
    scheduler.schedules_path.write_text("{not valid json", encoding="utf-8")

    # A read-only view degrades to empty instead of crashing.
    assert scheduler.list_backups() == []

    # A mutating caller preserves the bad bytes, then starts fresh (the data is
    # already unrecoverable — refusing forever would only wedge the scheduler).
    scheduler.add_backup(_mk("fresh"))
    assert {b.name for b in scheduler.list_backups()} == {"fresh"}

    corrupt = scheduler.schedules_path.parent / (scheduler.schedules_path.name + ".corrupt")
    assert corrupt.exists()
    assert corrupt.read_text(encoding="utf-8") == "{not valid json"


# ---------------------------------------------------------------------------
# Missing / empty are genuinely empty (the states {} is CORRECT for).
# ---------------------------------------------------------------------------


def test_missing_file_is_empty(scheduler):
    assert not scheduler.schedules_path.exists()
    assert scheduler.list_backups() == []


def test_empty_file_is_empty_not_error(scheduler):
    scheduler.schedules_path.write_text("   \n", encoding="utf-8")
    assert scheduler.list_backups() == []
    scheduler.add_backup(_mk("solo"))
    assert {b.name for b in scheduler.list_backups()} == {"solo"}


# ---------------------------------------------------------------------------
# Atomic write (delegated to json_io.atomic_write_json): a crash mid-write leaves
# the previous good file, not a truncated one that would parse empty and arm the
# wipe next load. The atomic mechanics are unit-tested in core/tests/core/
# test_json_io.py; here we assert the scheduler integrates them correctly.
# ---------------------------------------------------------------------------


def test_save_failure_leaves_store_intact(scheduler, monkeypatch):
    scheduler.add_backup(_mk("keep"))

    # Force the atomic replace (inside json_io's atomic write) to fail.
    import os as _os

    def _boom(*_a, **_k):
        raise OSError("disk full during replace")

    monkeypatch.setattr(_os, "replace", _boom)
    with pytest.raises(OSError):
        scheduler.add_backup(_mk("doomed"))
    monkeypatch.undo()

    # The original file is untouched (still valid JSON with only "keep"), and no
    # stray temp file was left behind in the schedule dir.
    on_disk = json.loads(scheduler.schedules_path.read_text(encoding="utf-8"))
    assert list(on_disk["schedules"].keys()) == ["keep"]
    leftovers = [p.name for p in scheduler.schedule_dir.glob("*.navig~")]
    assert leftovers == [], f"temp files leaked: {leftovers}"
