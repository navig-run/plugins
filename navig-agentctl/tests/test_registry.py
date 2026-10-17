"""AgentCtlStore — session ledger + bindings.

Isolation: every test passes an explicit tmp db_path (never the real store).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from navig_agentctl.registry import AgentCtlStore


@pytest.fixture()
def store(tmp_path: Path) -> AgentCtlStore:
    return AgentCtlStore(db_path=tmp_path / "agentctl.db")


def _session(**over) -> dict:
    row = {
        "session_id": "sess-1",
        "pid": 1234,
        "project": "navig:agent-control-center",
        "project_root": "E:/projects/apps/navig",
        "worktree": "agent-control-center",
        "agent_kind": "claude-code",
        "editor": "vscode",
        "owner": "code[1234]",
        "state": "active",
        "reason": "tool running (bash)",
    }
    row.update(over)
    return row


def test_schema_version_is_stamped(store: AgentCtlStore):
    conn = sqlite3.connect(store.db_path)
    try:
        row = conn.execute("SELECT version FROM schema_version").fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == AgentCtlStore.SCHEMA_VERSION


def test_upsert_then_list_round_trip(store: AgentCtlStore):
    store.upsert_session(_session())
    rows = store.list_sessions()
    assert len(rows) == 1
    r = rows[0]
    assert r["session_id"] == "sess-1"
    assert r["state"] == "active"
    assert r["project"] == "navig:agent-control-center"
    assert r["worktree"] == "agent-control-center"
    assert r["first_seen_at"] and r["last_seen_at"]


def test_upsert_same_state_records_no_duplicate_event(store: AgentCtlStore):
    store.upsert_session(_session())
    store.upsert_session(_session())
    events = store.session_events("sess-1")
    assert len(events) == 1  # a re-snapshot of the same state is not history


def test_state_change_appends_event(store: AgentCtlStore):
    store.upsert_session(_session(state="active"))
    store.upsert_session(_session(state="idle", reason="transcript untouched 25h"))
    events = store.session_events("sess-1")
    assert [e["state"] for e in events] == ["idle", "active"]  # newest first
    assert events[0]["detail"] == "transcript untouched 25h"


def test_events_are_capped(store: AgentCtlStore):
    for i in range(30):
        store.upsert_session(_session(state=f"s{i}"))
    events = store.session_events("sess-1", limit=100)
    assert len(events) <= 20


def test_mark_stale_only_touches_old_rows(store: AgentCtlStore):
    store.upsert_session(_session())
    assert store.mark_stale() == 0  # just-seen is not stale
    conn = sqlite3.connect(store.db_path)
    try:
        conn.execute(
            "UPDATE session SET last_seen_at = '2000-01-01T00:00:00.000000Z' WHERE session_id = 'sess-1'"
        )
        conn.commit()
    finally:
        conn.close()
    assert store.mark_stale() == 1
    assert store.get_session("sess-1")["state"] == "stale"


def test_bind_release_lifecycle(store: AgentCtlStore):
    store.bind("board:abc", "sess-1", note="working on it")
    assert store.binding_for("board:abc")["session_id"] == "sess-1"
    # rebinding moves the owner and clears the release
    store.bind("board:abc", "sess-2")
    assert store.binding_for("board:abc")["session_id"] == "sess-2"
    store.release("board:abc")
    assert store.binding_for("board:abc") is None
    assert len(store.bindings(active_only=False)) == 1  # ledger keeps history


def test_empty_session_id_is_ignored(store: AgentCtlStore):
    store.upsert_session(_session(session_id=""))
    assert store.list_sessions() == []


# ── annotate_session (the Phase-2a heartbeat write path) ─────────────────────


def test_annotate_creates_unknown_row_with_event(store: AgentCtlStore):
    assert store.annotate_session(
        {"session_id": "hb-1", "owner": "roster", "editor": "vscode"}
    )
    row = store.get_session("hb-1")
    assert row["state"] == "unknown"  # daemon hasn't classified it yet
    assert row["pid"] is None
    assert row["owner"] == "roster"
    assert row["first_seen_at"] and row["last_seen_at"]
    events = store.session_events("hb-1")
    assert events[0]["state"] == "unknown"
    assert "heartbeat" in events[0]["detail"]


def test_annotate_preserves_daemon_owned_fields(store: AgentCtlStore):
    store.upsert_session(_session(state="active", pid=77, reason="tool running"))
    store.annotate_session(
        {"session_id": "sess-1", "owner": "roster-y", "state": "idle", "pid": 1}
    )
    row = store.get_session("sess-1")
    assert row["state"] == "active"
    assert row["pid"] == 77
    assert row["reason"] == "tool running"
    assert row["owner"] == "roster-y"


def test_annotate_empty_string_never_erases_known_annotation(store: AgentCtlStore):
    store.annotate_session({"session_id": "hb-2", "worktree": "known-wt"})
    store.annotate_session({"session_id": "hb-2", "worktree": ""})  # "I don't know"
    assert store.get_session("hb-2")["worktree"] == "known-wt"


def test_annotate_does_not_refresh_last_seen(store: AgentCtlStore):
    """A heartbeating dead session must still age into 'stale' — only the
    daemon's snapshot refreshes last_seen_at."""
    import sqlite3

    store.annotate_session({"session_id": "hb-3"})
    conn = sqlite3.connect(store.db_path)
    try:
        conn.execute(
            "UPDATE session SET last_seen_at = '2000-01-01T00:00:00.000000Z' "
            "WHERE session_id = 'hb-3'"
        )
        conn.commit()
    finally:
        conn.close()
    store.annotate_session({"session_id": "hb-3", "owner": "still-alive"})
    assert store.get_session("hb-3")["last_seen_at"].startswith("2000-")
    assert store.mark_stale() == 1  # it goes stale despite the heartbeat
    assert store.get_session("hb-3")["state"] == "stale"


def test_annotate_empty_session_id_is_ignored(store: AgentCtlStore):
    assert not store.annotate_session({"session_id": "   ", "owner": "x"})
    assert store.list_sessions() == []
