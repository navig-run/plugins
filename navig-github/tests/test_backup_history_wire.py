"""MirrorOrchestrator records each real run into the backup-history store, so the
`navig github analytics` / `analytics-history` commands finally have data (they read
``<dest>/.navig-github-history.json`` — the same file record_backup writes).

Before this wire, record_backup had zero callers, so those surfaces were always empty
("No backup history found") despite the command help promising "History is recorded after
backup operations".
"""

from __future__ import annotations

from navig_github.engine.analytics import BackupAnalytics
from navig_github.engine.mirror import MirrorOrchestrator
from navig_github.engine.models import Config, MirrorSummary, TargetType


def _orch(dest):
    return MirrorOrchestrator(Config(target_type=TargetType.USER, target_name="me", dest=dest))


def _summary(cloned=0, updated=0, failed=0, errors=None):
    s = MirrorSummary()
    s.cloned, s.updated, s.failed = cloned, updated, failed
    s.errors = errors or []
    return s


def test_record_history_feeds_the_analytics_reader(tmp_path):
    _orch(tmp_path)._record_history(_summary(cloned=2, updated=1), duration_seconds=3.5)

    # The analytics command reads the SAME file — so it now sees this run.
    hist = BackupAnalytics(tmp_path).get_history()
    assert len(hist) == 1
    h = hist[0]
    assert h.repos_cloned == 2 and h.repos_updated == 1 and h.repos_failed == 0
    assert h.duration_seconds == 3.5
    assert h.success is True
    assert h.error_message is None


def test_record_history_captures_partial_failure(tmp_path):
    _orch(tmp_path)._record_history(
        _summary(cloned=1, failed=2, errors=["a/b: boom", "c/d: nope"]), duration_seconds=1.0
    )
    h = BackupAnalytics(tmp_path).get_history()[0]
    assert h.repos_failed == 2 and h.success is False
    assert "boom" in (h.error_message or "")


def test_record_history_accumulates_across_runs(tmp_path):
    orch = _orch(tmp_path)
    orch._record_history(_summary(cloned=1), duration_seconds=1.0)
    orch._record_history(_summary(updated=3), duration_seconds=2.0)
    assert len(BackupAnalytics(tmp_path).get_history()) == 2


def test_record_history_never_raises(tmp_path, monkeypatch):
    """Analytics is a side concern — a failure recording history must never break a backup."""
    import navig_github.engine.analytics as analytics_mod

    class _Boom:
        def __init__(self, *_a, **_k):
            raise RuntimeError("disk exploded")

    monkeypatch.setattr(analytics_mod, "BackupAnalytics", _Boom)
    # Must swallow the error and return normally.
    _orch(tmp_path)._record_history(_summary(cloned=1), duration_seconds=1.0)
