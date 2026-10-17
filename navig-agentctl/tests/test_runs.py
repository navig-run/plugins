"""Run ledger — state machine honesty, idempotency, sweeper, redaction."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest
from navig_agentctl.registry import AgentCtlStore
from navig_agentctl.redact import MASK, redact


@pytest.fixture()
def store(tmp_path):
    return AgentCtlStore(db_path=tmp_path / "agentctl.db")


def ev(rid="r1", type="start", **kw):
    return {"run_id": rid, "type": type, **kw}


def test_start_then_end_succeeded_is_a_done_transition(store):
    store.apply_event(ev(kind="bash", label="build", command="npm run build"))
    out = store.apply_event(ev(type="end", state="succeeded", exit_code=0, verified=True))
    assert out["run"]["state"] == "succeeded"
    assert out["run"]["exit_code"] == 0 and out["run"]["verified"] is True
    assert out["transition"]["kind"] == "done"


def test_failed_end_carries_the_exit_code(store):
    store.apply_event(ev(kind="bash"))
    out = store.apply_event(ev(type="end", state="failed", exit_code=3))
    assert out["run"]["state"] == "failed" and out["run"]["exit_code"] == 3
    assert out["transition"]["kind"] == "failed"


def test_events_are_idempotent_on_event_id(store):
    store.apply_event(ev(kind="bash"))
    first = store.apply_event(ev(type="end", state="failed", exit_code=1, event_id="e-1"))
    again = store.apply_event(ev(type="end", state="failed", exit_code=1, event_id="e-1"))
    assert first["duplicate"] is False and again["duplicate"] is True
    assert again["transition"] is None  # no second notification


def test_terminal_state_is_final(store):
    store.apply_event(ev(kind="bash"))
    store.apply_event(ev(type="end", state="succeeded", exit_code=0))
    out = store.apply_event(ev(type="activity"))
    out2 = store.apply_event(ev(type="end", state="failed", exit_code=9))
    assert out["run"]["state"] == "succeeded"
    assert out2["run"]["state"] == "succeeded" and out2["run"]["exit_code"] == 0


def test_turn_end_never_completes_a_run(store):
    """Stop = a conversation turn finished; the session (and its work) is still live."""
    store.apply_event(ev(kind="session"))
    out = store.apply_event(ev(type="turn_end", detail="turn finished"))
    assert out["run"]["state"] == "running"
    assert out["run"]["phase"] == "idle"
    assert out["transition"] is None


def test_unknown_run_is_created_by_a_late_event(store):
    """A hook installed mid-session never saw SessionStart — the first event creates the run."""
    out = store.apply_event(ev("cc:late", type="activity", kind="session"))
    assert out["run"]["state"] == "running" and out["run"]["kind"] == "session"


def test_needs_input_then_activity_resumes(store):
    store.apply_event(ev(kind="session"))
    out = store.apply_event(ev(type="needs_input", detail="permission prompt"))
    assert out["run"]["state"] == "needs_input" and out["transition"]["kind"] == "needs_input"
    assert store.apply_event(ev(type="needs_input"))["transition"] is None  # no repeat ping
    assert store.apply_event(ev(type="activity"))["run"]["state"] == "running"


def test_progress_is_never_invented(store):
    store.apply_event(ev(kind="bash"))
    out = store.apply_event(ev(type="stage", stage={"name": "compile", "state": "done"}))
    assert out["run"]["stages_total"] is None  # nothing declared → no denominator
    assert out["run"]["stages_done"] == 1


def test_declared_stages_give_a_real_denominator(store):
    store.apply_event(ev(kind="land", stages_planned=["rebase", "gate", "push"]))
    out = store.apply_event(ev(type="stage", stage={"name": "rebase", "state": "done"}))
    assert (out["run"]["stages_done"], out["run"]["stages_total"]) == (1, 3)


def test_sweep_flags_a_stall_only_when_the_process_is_alive_and_working(store):
    """Silence alone proves nothing: pid-less (hook-only) runs are never flagged."""
    store.apply_event(ev("w", kind="bash", stall_after_s=60, pid=os.getpid()))
    store.apply_event(ev("n", kind="bash", stall_after_s=60))  # no pid — a silent turn, not a hang
    store.apply_event(ev("i", kind="session", stall_after_s=60, pid=os.getpid()))
    store.apply_event(ev("i", type="turn_end"))  # waiting for the human
    later = datetime.now(timezone.utc) + timedelta(seconds=120)
    swept = store.sweep_runs(now=later)
    assert [(s["run"]["run_id"], s["kind"]) for s in swept] == [("w", "stalled")]
    assert store.get_run("n")["state"] == "running"
    assert store.get_run("i")["state"] == "running"  # idle session waiting for the user ≠ stalled
    assert store.apply_event(ev("w", type="activity"))["run"]["state"] == "running"  # activity clears it


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_sweep_marks_a_vanished_process_interrupted_not_failed(store):
    store.apply_event(ev("p", kind="navig", pid=_dead_pid()))
    swept = store.sweep_runs()
    assert swept and swept[0]["kind"] == "failed"
    run = store.get_run("p")
    assert run["state"] == "interrupted" and "vanished" in run["detail"]
    assert run["exit_code"] is None  # we do not know one


def test_a_live_pid_is_left_alone(store):
    store.apply_event(ev("p", kind="navig", pid=os.getpid()))
    assert store.sweep_runs() == []
    assert store.get_run("p")["state"] == "running"


def test_launched_runs_are_never_stall_flagged_and_expire_as_ended(store):
    """An unknown finish is not a failure: a hook-only launch closes neutrally."""
    store.apply_event(ev("b", kind="bash", background=True))
    assert store.sweep_runs(now=datetime.now(timezone.utc) + timedelta(hours=1)) == []
    assert store.sweep_runs(now=datetime.now(timezone.utc) + timedelta(hours=7)) == []  # no failure ping
    run = store.get_run("b")
    assert run["state"] == "ended" and "never observed" in run["detail"]
    assert run["exit_code"] is None and run["ended_at"]


def test_a_stall_claim_that_lost_its_pid_is_retracted(store):
    """A row flagged by an older build (no pid to prove liveness with) must clear."""
    store.apply_event(ev("c", kind="bash", stall_after_s=5))
    store._update_run("c", {"state": "stalled", "detail": "no activity for 1 min"})
    store.sweep_runs(now=datetime.now(timezone.utc) + timedelta(hours=1))
    run = store.get_run("c")
    assert run["state"] == "running" and run["detail"] == ""


def test_summary_attention_priority_and_ack(store):
    assert store.runs_summary()["attention"] == "idle"
    store.apply_event(ev("a", kind="bash"))
    assert store.runs_summary()["attention"] == "running"
    store.apply_event(ev("f", kind="bash"))
    store.apply_event(ev("f", type="end", state="failed", exit_code=2))
    assert store.runs_summary()["attention"] == "failed"
    assert store.ack_run("f") is True
    assert store.runs_summary()["attention"] == "running"


def test_secrets_are_redacted_on_the_way_in(store):
    store.apply_event(ev(kind="bash", command="curl -H 'Authorization: Bearer abcdef1234567890' --password hunter2 https://u:pw@h/x"))
    cmd = store.get_run("r1")["command"]
    assert "hunter2" not in cmd and "abcdef1234567890" not in cmd and ":pw@" not in cmd


@pytest.mark.parametrize(
    "raw,leak",
    [
        ("GITHUB_TOKEN=ghp_" + "a" * 36, "ghp_"),
        ("export API_KEY='s3cr3t-value'", "s3cr3t-value"),
        ("sk-" + "x" * 30, "sk-xxx"),
        ("--token abc123def456", "abc123def456"),
        ("12345678:" + "A" * 35, "AAAAAAAA"),
        ("$env:DB_PASSWORD = 'p@ss'", "p@ss"),
    ],
)
def test_redact_masks_credentials(raw, leak):
    out = redact(raw)
    assert leak not in out and MASK in out


def test_redact_keeps_ordinary_text():
    assert redact("npm run build -- --watch") == "npm run build -- --watch"


def test_schema_v1_db_migrates_additively(tmp_path):
    """A v1 database (sessions + bindings, no run tables) gains the ledger and keeps its rows."""
    import sqlite3

    path = tmp_path / "old.db"
    s = AgentCtlStore(db_path=path)
    s.upsert_session({"session_id": "keep-me", "state": "idle"})
    conn = sqlite3.connect(path)
    conn.executescript("DROP TABLE run; DROP TABLE run_event; UPDATE schema_version SET version = 1;")
    conn.commit()
    conn.close()
    again = AgentCtlStore(db_path=path)
    again.apply_event(ev(kind="bash"))
    assert again.get_run("r1")["state"] == "running"
    assert again.get_session("keep-me")["state"] == "idle"
    version = sqlite3.connect(path).execute("SELECT version FROM schema_version").fetchone()[0]
    assert version == AgentCtlStore.SCHEMA_VERSION


def test_stall_detail_reads_in_seconds_when_short_and_refreshes(store):
    store.apply_event(ev("s", kind="bash", stall_after_s=5, pid=os.getpid()))
    t0 = datetime.now(timezone.utc) + timedelta(seconds=30)
    store.sweep_runs(now=t0)
    first = store.get_run("s")["detail"]
    assert first.startswith("no activity for ") and "0 min" not in first
    # the claim must never freeze: a later sweep reports the real idle time
    store.sweep_runs(now=t0 + timedelta(hours=2))
    assert store.get_run("s")["detail"] != first
    assert store.get_run("s")["detail"].startswith("no activity for ")


def test_summary_never_counts_interrupted_as_failed(store):
    """The Dock prints this number next to 'failed' — an unknown/died finish must not inflate it."""
    store.apply_event(ev("k", kind="bash"))
    store.apply_event(ev("k", type="end", state="succeeded", exit_code=0))
    store.apply_event(ev("d", kind="navig", pid=_dead_pid()))
    store.sweep_runs()
    assert store.get_run("d")["state"] == "interrupted"
    assert store.runs_summary()["failed_unacked"] == 0
    assert store.runs_summary()["attention"] != "failed"


def test_redact_keeps_long_identifiers_readable():
    """A 48+ char snake_case name is not a credential (it hid failing test names in a gate log)."""
    name = "test_malformed_events_are_rejected_not_partially_written"
    assert redact(f"FAILED tests/x.py::{name}[body3]") == f"FAILED tests/x.py::{name}[body3]"
    assert MASK in redact("token blob " + "a1B2c3D4e5" * 6)
