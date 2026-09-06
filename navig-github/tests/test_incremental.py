"""IncrementalBackupManager persists a per-backup-dir state cache. It's a single-object
replace (no reload-merge, so no sibling-wipe class), but the write must be atomic — a
torn state file reads as corrupt next run and silently forces a full (non-incremental)
re-backup. Also covers the pre-0.2.3 state-filename fallback so an existing backup dir
keeps its incremental state across the rename.
"""

from __future__ import annotations

import json

from navig_github.engine.incremental import BackupState, IncrementalBackupManager


def _state(name: str = "octocat") -> BackupState:
    return BackupState(
        last_backup="2026-01-01T00:00:00+00:00",
        target_type="user",
        target_name=name,
        repos_backed_up=["a/b"],
    )


def test_roundtrip_new_filename(tmp_path):
    mgr = IncrementalBackupManager(tmp_path)
    mgr.save_state(_state())
    loaded = mgr.load_state()
    assert loaded is not None and loaded.repos_backed_up == ["a/b"]
    assert (tmp_path / ".navig-github_state.json").exists()  # debranded name


def test_legacy_state_filename_is_read(tmp_path):
    # a backup dir written by the pre-rename engine keeps its incremental state
    (tmp_path / ".the engine_state.json").write_text(
        json.dumps(_state().to_dict()), encoding="utf-8"
    )
    mgr = IncrementalBackupManager(tmp_path)
    loaded = mgr.load_state()
    assert loaded is not None and loaded.target_name == "octocat"


def test_corrupt_state_returns_none(tmp_path):
    (tmp_path / ".navig-github_state.json").write_text("{not valid json", encoding="utf-8")
    mgr = IncrementalBackupManager(tmp_path)
    assert mgr.load_state() is None  # graceful degrade → next run is a full backup
