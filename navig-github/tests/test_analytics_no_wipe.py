"""A transient/unreadable read of the backup-history store must NOT wipe recorded backups.

BackupAnalytics loads .navig-github-history.json in __init__; the old _load_history caught
only (JSONDecodeError, TypeError), so a truncated file (from the old NON-atomic write) read
back as corrupt → [], and record_backup then appended one entry and saved it over the whole
history. (record_backup has no live caller yet — this hardens it to the same contract as its
sibling store TemplateManager, #571, so it is safe the moment a backup run is wired to it.)

Now _load_history reads via json_io.load_json_for_update (RAISES JsonReadError on an
unreadable-but-populated file, quarantines a corrupt one), sets _history_load_failed, and
_save_history REFUSES while flagged. _save_history is atomic, so it can't truncate.
"""

from __future__ import annotations

import navig.core.json_io as jio
from navig_github.engine.analytics import BackupAnalytics


def _record(a: BackupAnalytics, cloned: int):
    return a.record_backup(repos_cloned=cloned, repos_updated=0, repos_failed=0,
                           duration_seconds=1.0, total_size_bytes=100)


def _seed_two(backup_dir):
    a = BackupAnalytics(backup_dir=backup_dir)
    _record(a, 3)
    _record(a, 5)
    return a


def test_record_backup_round_trip(tmp_path):
    _seed_two(tmp_path)
    fresh = BackupAnalytics(backup_dir=tmp_path)
    assert [h.repos_cloned for h in fresh._history] == [3, 5]


def test_record_during_lock_refuses_save_without_wiping(monkeypatch, tmp_path):
    _seed_two(tmp_path)
    store = tmp_path / BackupAnalytics.HISTORY_FILE
    before = store.read_text(encoding="utf-8")
    assert '"repos_cloned": 3' in before and '"repos_cloned": 5' in before

    def _locked(*_a, **_k):
        raise OSError("file is locked")

    monkeypatch.setattr(jio, "read_text_retrying", _locked)

    a = BackupAnalytics(backup_dir=tmp_path)   # construction load hits the lock
    assert a._history_load_failed is True
    _record(a, 9)                              # append + save must be REFUSED

    # Store on disk intact (read directly — Path.read_text, not the patched json_io).
    assert store.read_text(encoding="utf-8") == before

    monkeypatch.undo()
    fresh = BackupAnalytics(backup_dir=tmp_path)  # lock lifted → real history returns
    assert [h.repos_cloned for h in fresh._history] == [3, 5]  # the 9 never persisted
