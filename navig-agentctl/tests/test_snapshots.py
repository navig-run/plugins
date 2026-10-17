"""Snapshot normalisers — pure logic, no live processes required."""

from __future__ import annotations

from pathlib import Path

from navig_agentctl.snapshots import _editor_of, _work_deps, record_snapshot
from navig_agentctl.registry import AgentCtlStore


def test_editor_of_recognises_editors():
    assert _editor_of("code[1234]") == "vscode"
    assert _editor_of("Code - Insiders[99]") == "vscode"
    assert _editor_of("cursor[42]") == "cursor"
    assert _editor_of("") == ""
    assert _editor_of("claude[7]") == ""  # CLI session, no editor owner


def test_work_deps_reads_metadata_json():
    row = {"metadata_json": '{"depends_on": ["auth-fix", "layout-fix"]}'}
    assert _work_deps(row) == ["work:auth-fix", "work:layout-fix"]


def test_work_deps_survives_malformed_metadata():
    assert _work_deps({"metadata_json": "{not json"}) == []
    assert _work_deps({"metadata_json": ""}) == []
    assert _work_deps({"metadata_json": "{}"}) == []


def test_record_snapshot_persists_and_skips_bad_rows(tmp_path: Path):
    store = AgentCtlStore(db_path=tmp_path / "agentctl.db")
    rows = [
        {"session_id": "ok-1", "state": "active", "project": "p"},
        {"session_id": "", "state": "active"},  # dropped
        {"session_id": "ok-2", "state": "idle", "project": "p"},
    ]
    n = record_snapshot(rows, store)
    assert n == 2
    ids = {s["session_id"] for s in store.list_sessions()}
    assert ids == {"ok-1", "ok-2"}


def test_worktree_snapshot_shape(tmp_path: Path):
    """A non-repo dir yields an empty list, never an exception."""
    from navig_agentctl.snapshots import worktree_snapshot

    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert worktree_snapshot(plain) == []
