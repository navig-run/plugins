"""Claude Code hook adapter — event mapping, fail-open delivery, offline spool."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest
from navig_agentctl import hook, spool

SID = "sess-1"


def payload(name, **kw):
    return {"hook_event_name": name, "session_id": SID, "cwd": "E:/projects/apps/navig", **kw}


def types(events):
    return [(e["run_id"], e["type"]) for e in events]


def test_stop_is_a_turn_end_never_a_run_end():
    evs = hook.claude_code_events(payload("Stop"))
    assert types(evs) == [(f"cc:{SID}", "turn_end")]
    assert not any(e["type"] == "end" for e in evs)


def test_subagent_stop_does_not_end_anything():
    evs = hook.claude_code_events(payload("SubagentStop"))
    assert all(e["type"] == "activity" for e in evs)


def test_session_lifecycle():
    assert types(hook.claude_code_events(payload("SessionStart"))) == [(f"cc:{SID}", "start")]
    end = hook.claude_code_events(payload("SessionEnd", reason="logout"))
    assert end[0]["type"] == "end" and end[0]["state"] == "ended"


def test_subagent_tool_use_opens_and_closes_a_child_run():
    pre = hook.claude_code_events(payload("PreToolUse", tool_name="Task", tool_use_id="tu1",
                                          tool_input={"description": "audit the repo"}))
    child = [e for e in pre if e["run_id"].endswith(":tu:tu1")]
    assert child and child[0]["kind"] == "subagent" and child[0]["parent_run_id"] == f"cc:{SID}"
    ok = hook.claude_code_events(payload("PostToolUse", tool_name="Task", tool_use_id="tu1", tool_input={}))
    assert any(e["run_id"].endswith(":tu:tu1") and e["state"] == "succeeded" for e in ok)
    bad = hook.claude_code_events(payload("PostToolUseFailure", tool_name="Task", tool_use_id="tu1",
                                          tool_input={}, error="boom"))
    assert any(e["run_id"].endswith(":tu:tu1") and e["state"] == "failed" for e in bad)


def test_a_failed_tool_is_not_a_failed_session():
    evs = hook.claude_code_events(payload("PostToolUseFailure", tool_name="Bash", tool_use_id="x",
                                          tool_input={"command": "false"}, error="exit 1"))
    assert types(evs) == [(f"cc:{SID}", "activity")]


def test_foreground_bash_does_not_create_a_run_but_background_does():
    fg = hook.claude_code_events(payload("PreToolUse", tool_name="Bash", tool_use_id="a",
                                         tool_input={"command": "npm test"}))
    assert types(fg) == [(f"cc:{SID}", "activity")]
    bg = hook.claude_code_events(payload("PreToolUse", tool_name="Bash", tool_use_id="b",
                                         tool_input={"command": "npm run land", "run_in_background": True}))
    started = [e for e in bg if e["type"] == "start"]
    assert started and started[0]["background"] is True and started[0]["kind"] == "bash"


def test_long_tools_get_a_larger_stall_allowance():
    long_ = hook.claude_code_events(payload("PreToolUse", tool_name="Bash", tool_use_id="a",
                                            tool_input={"command": "x"}))
    short = hook.claude_code_events(payload("PreToolUse", tool_name="Read", tool_use_id="a",
                                            tool_input={"file_path": "x"}))
    assert long_[0]["stall_after_s"] > short[0]["stall_after_s"]


def test_permission_prompt_needs_input_and_idle_does_not():
    need = hook.claude_code_events(payload("Notification", notification_type="permission_prompt",
                                           message="Claude needs your permission to use Bash"))
    assert need[0]["type"] == "needs_input"
    idle = hook.claude_code_events(payload("Notification", notification_type="idle_prompt", message="waiting"))
    assert idle[0]["type"] == "activity"


def test_task_completed_is_a_real_stage():
    evs = hook.claude_code_events(payload("TaskCompleted", task_id="3", task_subject="Wire the Dock pill"))
    assert evs[0]["type"] == "stage" and evs[0]["stage"] == {"name": "Wire the Dock pill", "state": "done"}


def test_secrets_in_tool_input_are_redacted_before_leaving():
    evs = hook.claude_code_events(payload(
        "PreToolUse", tool_name="Bash", tool_use_id="a",
        tool_input={"command": "curl -H 'Authorization: Bearer abcdef1234567890' --password hunter2 x"}))
    blob = json.dumps(evs)
    assert "abcdef1234567890" not in blob and "hunter2" not in blob


def test_junk_payloads_map_to_nothing():
    assert hook.claude_code_events({}) == []
    assert hook.claude_code_events({"hook_event_name": "Stop"}) == []
    assert hook.claude_code_events(payload("SomethingNew")) == []


@pytest.fixture()
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("NAVIG_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("NAVIG_AGENT_MONITOR_URL", raising=False)
    return tmp_path


def test_daemon_down_spools_and_drain_returns_events_once(isolated):
    (isolated / "gateway.json").write_text(json.dumps({"host": "127.0.0.1", "port": 9}), encoding="utf-8")
    evs = hook.claude_code_events(payload("Stop"))
    assert hook.deliver(evs) is True  # spooled counts as handled
    drained = spool.drain()
    assert [e["type"] for e in drained] == ["turn_end"]
    assert spool.drain() == []


def test_spool_is_bounded(isolated, monkeypatch):
    monkeypatch.setattr(spool, "MAX_SPOOL_BYTES", 200)
    results = [spool.append({"run_id": "r", "type": "activity", "detail": "x" * 80}) for _ in range(10)]
    assert results[0] is True and results[-1] is False


def test_main_is_fail_open_on_garbage_stdin(isolated, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("{not json"))
    assert hook.main(["--agent", "claude-code"]) == 0
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload("Stop"))))
    monkeypatch.setattr(hook, "deliver", lambda e: (_ for _ in ()).throw(RuntimeError("boom")))
    assert hook.main(["--agent", "claude-code"]) == 0


def test_the_hook_process_exits_zero_and_prints_nothing(isolated):
    proc = subprocess.run(
        [sys.executable, "-m", "navig_agentctl.hook", "--agent", "claude-code"],
        input=json.dumps(payload("Stop")), capture_output=True, text=True, timeout=20,
        cwd=str(Path(hook.__file__).resolve().parents[1]),  # import the code under test, not an installed copy
        env={**__import__("os").environ, "NAVIG_CONFIG_DIR": str(isolated)},
    )
    assert proc.returncode == 0 and proc.stdout == "" and proc.stderr == ""


def test_a_drain_past_the_cap_keeps_the_rest_for_the_next_pass(isolated, monkeypatch):
    """Regression: a 4892-event spool drained 2000 and DELETED the other 2892."""
    monkeypatch.setattr(spool, "MAX_DRAIN_EVENTS", 3)
    for i in range(7):
        assert spool.append({"run_id": "r", "type": "activity", "event_id": f"e{i}"})
    seen = []
    for _ in range(4):
        seen += [e["event_id"] for e in spool.drain()]
    assert seen == [f"e{i}" for i in range(7)]  # every event, in order, exactly once
    assert spool.drain() == []
