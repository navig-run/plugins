"""The managed-process runner against REAL subprocesses — exit codes, logs, verification, land."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest
from navig_agentctl import land_adapter, logs, runner
from navig_agentctl import registry as reg

PY = sys.executable


@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("NAVIG_CONFIG_DIR", str(tmp_path / "cfg"))  # no gateway.json → ledger written directly
    monkeypatch.delenv("NAVIG_AGENT_MONITOR_URL", raising=False)
    monkeypatch.setattr(reg, "_default_db_path", lambda: tmp_path / "agentctl.db")  # == NAVIG_STORE_DIR/agentctl.db
    monkeypatch.setattr(logs, "log_root", lambda: tmp_path / "logs" / "agent-runs")
    return reg.get_agentctl_store()


def code(src: str) -> list[str]:
    return [PY, "-c", src]


def test_success_records_exit_zero_log_and_timing(env, tmp_path):
    rc = runner.run_command(code("print('step 1'); print('step 2')"), run_id="run:ok", label="ok", cwd=tmp_path)
    run = env.get_run("run:ok")
    assert rc == 0 and run["state"] == "succeeded" and run["exit_code"] == 0
    assert run["ended_at"] and run["elapsed_s"] is not None and run["pid"]
    assert logs.tail(env.get_run_raw("run:ok")["log_path"]) == ["step 1", "step 2"]


def test_failure_records_the_real_exit_code(env, tmp_path):
    rc = runner.run_command(code("import sys; print('boom'); sys.exit(3)"), run_id="run:bad", cwd=tmp_path)
    run = env.get_run("run:bad")
    assert rc == 3 and run["state"] == "failed" and run["exit_code"] == 3


def test_unlaunchable_command_is_a_recorded_failure(env, tmp_path):
    rc = runner.run_command(["definitely-not-a-real-binary-xyz"], run_id="run:nf", cwd=tmp_path)
    assert rc == 127 and env.get_run("run:nf")["state"] == "failed"


def test_failing_verification_turns_exit_zero_into_failed(env, tmp_path):
    rc = runner.run_command(code("print('done')"), run_id="run:v", cwd=tmp_path,
                            verify_cmd=f'"{PY}" -c "import sys; sys.exit(1)"')
    run = env.get_run("run:v")
    assert rc == 1 and run["state"] == "failed" and run["verified"] is False and "verify failed" in run["detail"]


def test_passing_verification_is_recorded(env, tmp_path):
    runner.run_command(code("pass"), run_id="run:v2", cwd=tmp_path, verify_cmd=f'"{PY}" -c "pass"')
    run = env.get_run("run:v2")
    assert run["state"] == "succeeded" and run["verified"] is True


def test_secrets_in_output_never_reach_the_log(env, tmp_path):
    runner.run_command(code("print('API_KEY=sk-live-aaaaaaaaaaaaaaaaaaaa'); print('ok')"), run_id="run:s", cwd=tmp_path)
    text = Path(env.get_run_raw("run:s")["log_path"]).read_text(encoding="utf-8")
    assert "sk-live" not in text and "ok" in text


def test_live_activity_and_pid_are_visible_while_running(env, tmp_path):
    proc = subprocess.Popen(
        [PY, "-m", "navig_agentctl.runner", "--run-id", "run:live", "--cwd", str(tmp_path), "--",
         PY, "-u", "-c", "import time; print('working', flush=True); time.sleep(6)"],
        cwd=str(Path(runner.__file__).resolve().parents[1]),
        # Hermetic: a child process does not inherit the in-process monkeypatches, so every
        # location it could write (config, store, logs) is pointed at tmp explicitly.
        env={**__import__("os").environ, "NAVIG_CONFIG_DIR": str(tmp_path / "cfg"),
             "NAVIG_STORE_DIR": str(tmp_path), "NAVIG_LOG_DIR": str(tmp_path / "logs"),
             "PYTHONPATH": str(Path(runner.__file__).resolve().parents[1])},
    )
    try:
        deadline = time.time() + 15
        run = None
        while time.time() < deadline:
            run = env.get_run("run:live")
            if run and run["state"] == "running" and run["pid"]:
                break
            time.sleep(0.2)
        assert run and run["state"] == "running" and run["pid"] and run["idle_s"] is not None
    finally:
        proc.wait(timeout=30)
    assert env.get_run("run:live")["state"] == "succeeded"


def test_killed_process_is_swept_as_interrupted_not_succeeded(env, tmp_path):
    from datetime import datetime, timedelta, timezone

    child = subprocess.Popen([PY, "-c", "import time; time.sleep(60)"])
    env.apply_event({"run_id": "run:k", "type": "start", "kind": "navig", "pid": child.pid})
    child.kill()
    child.wait()
    swept = env.sweep_runs(now=datetime.now(timezone.utc) + timedelta(seconds=1))
    assert swept and env.get_run("run:k")["state"] == "interrupted"


# ── land adapter ────────────────────────────────────────────────────────────────

LAND_SCRIPT = r"""
import json, os, sys
path = os.environ["NAVIG_LAND_EVENTS"]
def emit(**o):
    with open(path, "a", encoding="utf-8") as f: f.write(json.dumps(o) + "\n")
emit(stage="preflight", state="done")
emit(stage="rebase", state="done")
emit(stage="gate", state="running", detail="attempt 1/5")
emit(stage="gate", state="failed", detail="main moved")
emit(stage="gate", state="running", detail="attempt 2/5")
emit(stage="gate", state="done", detail="attempt 2/5")
emit(stage="push", state="done", meta={"sha": "%(sha)s", "branch": "feat/x"})
emit(stage="result", state="done", meta={"outcome": "%(outcome)s", "sha": "%(sha)s", "branch": "feat/x"})
"""


def test_land_stages_come_from_the_script_not_the_console(env, tmp_path, monkeypatch):
    monkeypatch.setattr(land_adapter, "verify", lambda cwd, branch, sha: (True, f"{sha[:9]} verified on origin/main"))
    src = LAND_SCRIPT % {"sha": "a" * 40, "outcome": "landed"}
    rc = runner.run_command(code(src), run_id="run:land", adapter="land", cwd=tmp_path)
    run = env.get_run("run:land")
    assert rc == 0 and run["kind"] == "land" and run["state"] == "succeeded"
    assert run["verified"] is True and "verified" in run["detail"]
    states = {s["name"]: s["state"] for s in run["stages"]}
    assert states["rebase"] == "done" and states["gate"] == "done" and states["queue"] == "pending"
    assert run["stages_total"] == len(land_adapter.STAGES)
    history = " | ".join(e["detail"] for e in env.run_events("run:land"))
    assert "gate: failed main moved" in history  # the retry is in the ledger, not lost


def test_land_exit_zero_without_git_proof_is_a_failure(env, tmp_path, monkeypatch):
    monkeypatch.setattr(land_adapter, "verify", lambda cwd, branch, sha: (False, "landed commit aaaaaaaaa is not on origin/main"))
    rc = runner.run_command(code(LAND_SCRIPT % {"sha": "a" * 40, "outcome": "landed"}),
                            run_id="run:land2", adapter="land", cwd=tmp_path)
    run = env.get_run("run:land2")
    assert rc == 1 and run["state"] == "failed" and run["verified"] is False and run["exit_code"] == 0


def test_land_nothing_to_land_is_success_without_a_verification_claim(env, tmp_path):
    runner.run_command(code(LAND_SCRIPT % {"sha": "", "outcome": "nothing"}),
                       run_id="run:land3", adapter="land", cwd=tmp_path)
    run = env.get_run("run:land3")
    assert run["state"] == "succeeded" and run["verified"] is None and "nothing to land" in run["detail"]


def test_read_new_lines_waits_for_a_complete_line(tmp_path):
    f = tmp_path / "e.jsonl"
    f.write_bytes(b'{"stage": "gate"}\n{"stage": "pu')
    got, off = land_adapter.read_new_lines(f, 0)
    assert [g["stage"] for g in got] == ["gate"]
    f.write_bytes(f.read_bytes() + b'sh"}\n')
    got2, _ = land_adapter.read_new_lines(f, off)
    assert [g["stage"] for g in got2] == ["push"]


def test_verify_against_a_real_git_remote(tmp_path):
    def git(cwd, *a):
        return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()

    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    git(tmp_path, "clone", str(remote), str(work))
    git(work, "config", "user.email", "t@t")
    git(work, "config", "user.name", "t")
    (work / "a.txt").write_text("a", encoding="utf-8")
    git(work, "add", "a.txt")
    git(work, "commit", "-m", "one")
    git(work, "push", "origin", "HEAD:main")
    sha = git(work, "rev-parse", "HEAD")
    ok, why = land_adapter.verify(work, "feat/gone", sha)
    assert ok, why
    (work / "b.txt").write_text("b", encoding="utf-8")
    git(work, "add", "b.txt")
    git(work, "commit", "-m", "unpushed")
    ok, why = land_adapter.verify(work, "feat/gone", git(work, "rev-parse", "HEAD"))
    assert not ok and "not on origin/main" in why
    assert land_adapter.verify(work, "", "")[0] is False
