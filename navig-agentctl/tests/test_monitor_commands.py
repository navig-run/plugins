"""CLI: hooks install/uninstall are diff-first and non-destructive; runs/run work end to end."""

from __future__ import annotations

import json
import sys

import pytest
from navig_agentctl import logs
from navig_agentctl import registry as reg
from navig_agentctl.commands import agentctl_app
from typer.testing import CliRunner

runner = CliRunner()


@pytest.fixture()
def env(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("NAVIG_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("NAVIG_AGENT_MONITOR_URL", raising=False)
    monkeypatch.setattr(reg, "_default_db_path", lambda: tmp_path / "agentctl.db")
    monkeypatch.setattr(logs, "log_root", lambda: tmp_path / "logs" / "agent-runs")
    return home


def settings(home):
    return home / ".claude" / "settings.json"


def test_install_is_a_dry_run_by_default(env):
    out = runner.invoke(agentctl_app, ["hooks", "install"])
    assert out.exit_code == 0 and "Dry run" in out.output and "navig_agentctl.hook" in out.output
    assert not settings(env).exists()


def test_install_writes_all_events_and_preserves_other_hooks(env):
    existing = {"model": "x", "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "guard.sh"}]}]}}
    settings(env).parent.mkdir(parents=True)
    settings(env).write_text(json.dumps(existing), encoding="utf-8")
    out = runner.invoke(agentctl_app, ["hooks", "install", "--yes"])
    assert out.exit_code == 0
    data = json.loads(settings(env).read_text(encoding="utf-8"))
    assert data["model"] == "x"
    pre = data["hooks"]["PreToolUse"]
    assert any(h["command"] == "guard.sh" for g in pre for h in g["hooks"])  # untouched
    for ev in ("PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop", "SubagentStop", "TaskCompleted",
               "SessionStart", "SessionEnd", "Notification", "UserPromptSubmit"):
        assert any("navig_agentctl.hook" in h["command"] for g in data["hooks"][ev] for h in g["hooks"]), ev
    assert (settings(env).parent / "settings.json.navig-bak").exists()
    cmd = [h["command"] for g in pre for h in g["hooks"] if "navig_agentctl" in h["command"]][0]
    assert "\\" not in cmd  # forward slashes: survives a POSIX-ish hook shell on Windows


def test_install_twice_is_idempotent(env):
    runner.invoke(agentctl_app, ["hooks", "install", "--yes"])
    first = settings(env).read_text(encoding="utf-8")
    again = runner.invoke(agentctl_app, ["hooks", "install", "--yes"])
    assert "nothing to change" in again.output and settings(env).read_text(encoding="utf-8") == first


def test_uninstall_removes_only_ours(env):
    existing = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine.sh"}]}]}}
    settings(env).parent.mkdir(parents=True)
    settings(env).write_text(json.dumps(existing), encoding="utf-8")
    runner.invoke(agentctl_app, ["hooks", "install", "--yes"])
    runner.invoke(agentctl_app, ["hooks", "uninstall", "--yes"])
    assert json.loads(settings(env).read_text(encoding="utf-8")) == existing


def test_invalid_settings_json_is_never_overwritten(env):
    settings(env).parent.mkdir(parents=True)
    settings(env).write_text("{ not json", encoding="utf-8")
    out = runner.invoke(agentctl_app, ["hooks", "install", "--yes"])
    assert out.exit_code == 1 and "not valid JSON" in out.output
    assert settings(env).read_text(encoding="utf-8") == "{ not json"


def test_run_records_success_and_failure_and_runs_lists_them(env, tmp_path):
    ok = runner.invoke(agentctl_app, ["run", "--label", "cli-ok", "--cwd", str(tmp_path), "--", sys.executable, "-c", "print('hi')"])
    bad = runner.invoke(agentctl_app, ["run", "--label", "cli-bad", "--cwd", str(tmp_path), "--", sys.executable, "-c", "import sys; sys.exit(4)"])
    assert ok.exit_code == 0 and "succeeded" in ok.output
    assert bad.exit_code == 4 and "failed" in bad.output
    rows = json.loads(runner.invoke(agentctl_app, ["runs", "--json"]).output)
    by = {r["label"]: r for r in rows}
    assert by["cli-ok"]["state"] == "succeeded" and by["cli-bad"]["exit_code"] == 4
    shown = runner.invoke(agentctl_app, ["show", by["cli-ok"]["run_id"], "--log"])
    assert "hi" in shown.output
    assert runner.invoke(agentctl_app, ["ack", "--all"]).exit_code == 0
    assert json.loads(runner.invoke(agentctl_app, ["runs", "--json"]).output)  # history survives ack


def test_run_without_a_command_is_a_usage_error(env):
    assert runner.invoke(agentctl_app, ["run"]).exit_code == 2


def test_runs_nudge_names_interrupted_separately_from_failed(env, tmp_path):
    """The nudge printed 'N failed/unacknowledged' counting interrupted runs (62 on live)."""
    import subprocess as sp

    from navig_agentctl.registry import get_agentctl_store

    runner.invoke(agentctl_app, ["run", "--cwd", str(tmp_path), "--", sys.executable, "-c", "import sys; sys.exit(3)"])
    p = sp.Popen([sys.executable, "-c", "pass"])  # a process that will vanish without reporting
    p.wait()
    store = get_agentctl_store()
    store.apply_event({"run_id": "run:vanished", "type": "start", "kind": "navig", "pid": p.pid})
    store.sweep_runs()

    out = runner.invoke(agentctl_app, ["runs"])
    assert "1 failed" in out.output, out.output
    assert "1 interrupted" in out.output, out.output
    assert "failed/unacknowledged" not in out.output
