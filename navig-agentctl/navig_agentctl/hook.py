"""Claude Code hook adapter for the Agent Monitor.

Usage (wired by ``navig agentctl hooks install``)::

    python -I -m navig_agentctl.hook --agent claude-code

Reads one hook payload on stdin, maps it to Agent Monitor events, POSTs them to
the local gateway and ALWAYS exits 0 with no output — a monitoring hook must
never block, alter or fail the agent it observes. If the gateway is down the
events are spooled to disk and drained by the daemon later.

Stdlib only and cold-start cheap. Everything that leaves this process has been
through :func:`navig_agentctl.redact.redact`.

Mapping (documented Claude Code hook events only):

=====================  =====================================================
SessionStart           session run starts (idle)
UserPromptSubmit       session turn starts (working)
PreToolUse             session activity + current tool; ``Task``/``Agent`` start
                       a subagent run; background ``Bash`` starts a ``launched`` run
PostToolUse            session activity; subagent run ends ``succeeded``
PostToolUseFailure     session activity (a failed tool is not a failed session);
                       subagent run ends ``failed``
Stop                   ``turn_end`` — a conversation turn finished, NOT the work
SubagentStop           session activity only (the subagent's own tool-use closes it)
TaskCompleted          a real completed stage on the session
Notification           permission / elicitation prompts → ``needs_input``
SessionEnd             session run ends
=====================  =====================================================

Background ``Bash`` completion is not observable through hooks. Those runs stay
``launched`` ("completion unknown") — wrap long commands with
``navig agentctl run -- <cmd>`` to get an exit code and a log.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from navig_agentctl.redact import redact

POST_TIMEOUT_S = 0.25
#: Tools whose in-flight time legitimately has no hook activity, so a stall is not suspected.
LONG_TOOLS = {"Bash", "PowerShell", "Task", "Agent"}
STALL_NORMAL_S = 900
STALL_LONG_TOOL_S = 3600
SUBAGENT_TOOLS = {"Task", "Agent"}


def _project(cwd: str) -> str:
    return Path(cwd).name if cwd else ""


def _tool_summary(name: str, tool_input: dict[str, Any]) -> str:
    """One short redacted line about what a tool is doing — never the full input."""
    for key in ("command", "file_path", "path", "pattern", "url", "description", "query"):
        val = tool_input.get(key)
        if isinstance(val, str) and val:
            return redact(f"{name}: {val}", limit=120)
    return name


def _ev(run_id: str, etype: str, **fields: Any) -> dict[str, Any]:
    ev: dict[str, Any] = {"event_id": uuid.uuid4().hex, "run_id": run_id, "type": etype}
    ev.update({k: v for k, v in fields.items() if v is not None and v != ""})
    return ev


def claude_code_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Map one Claude Code hook payload to Agent Monitor events (pure; no I/O)."""
    hook = str(payload.get("hook_event_name") or "")
    sid = str(payload.get("session_id") or "")
    if not sid or not hook:
        return []
    cwd = str(payload.get("cwd") or "")
    project = _project(cwd)
    run = f"cc:{sid}"
    base = {"session_id": sid, "project": project, "worktree": redact(cwd, limit=200)}
    tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    tool_use_id = str(payload.get("tool_use_id") or "")
    child = f"{run}:tu:{tool_use_id}" if tool_use_id else ""
    stall = STALL_LONG_TOOL_S if tool in LONG_TOOLS else STALL_NORMAL_S
    session_start = _ev(run, "start", kind="session", label=f"Claude Code · {project}" if project else "Claude Code",
                        phase="idle", **base)

    if hook == "SessionStart":
        return [session_start]
    if hook == "SessionEnd":
        return [_ev(run, "end", state="ended", detail=redact(payload.get("reason") or "session ended", limit=120))]
    if hook == "UserPromptSubmit":
        return [_ev(run, "activity", kind="session", phase="working", detail="working on a prompt", **base)]
    if hook == "Stop":
        return [_ev(run, "turn_end", kind="session", detail="turn finished — waiting for you", **base)]
    if hook == "SubagentStop":
        return [_ev(run, "activity", kind="session", detail="a subagent finished", **base)]
    if hook == "TaskCompleted":
        subject = redact(payload.get("task_subject") or payload.get("task_id") or "", limit=160)
        if not subject:
            return []
        return [_ev(run, "stage", kind="session", stage={"name": subject, "state": "done"}, **base)]
    if hook == "Notification":
        ntype = str(payload.get("notification_type") or "")
        message = redact(payload.get("message") or "", limit=200)
        needs = ntype in ("permission_prompt", "elicitation_dialog") or (
            not ntype and "permission" in message.lower()
        )
        return [_ev(run, "needs_input" if needs else "activity", kind="session",
                    detail=message or ntype, **base)]

    events: list[dict[str, Any]] = []
    if hook == "PreToolUse":
        events.append(_ev(run, "activity", kind="session", phase="working", stall_after_s=stall,
                          detail=_tool_summary(tool, tool_input), **base))
        if tool in SUBAGENT_TOOLS and child:
            events.append(_ev(child, "start", kind="subagent", parent_run_id=run,
                              label=redact(tool_input.get("description") or tool_input.get("subagent_type")
                                           or "subagent", limit=160),
                              stall_after_s=STALL_LONG_TOOL_S, **base))
        elif tool in ("Bash", "PowerShell") and tool_input.get("run_in_background") and child:
            cmd = redact(tool_input.get("command") or "", limit=400)
            events.append(_ev(child, "start", kind="bash", parent_run_id=run, background=True, command=cmd,
                              label=redact(tool_input.get("description") or cmd, limit=80), **base))
        return events
    if hook == "PostToolUse":
        events.append(_ev(run, "activity", kind="session", stall_after_s=STALL_NORMAL_S,
                          detail=_tool_summary(tool, tool_input), **base))
        if tool in SUBAGENT_TOOLS and child:
            events.append(_ev(child, "end", state="succeeded", detail="subagent returned"))
        return events
    if hook == "PostToolUseFailure":
        err = redact(payload.get("error") or "", limit=160)
        interrupted = bool(payload.get("is_interrupt"))
        events.append(_ev(run, "activity", kind="session", stall_after_s=STALL_NORMAL_S,
                          detail=f"{tool} {'interrupted' if interrupted else 'failed'}: {err}".strip(": "), **base))
        if tool in SUBAGENT_TOOLS and child:
            events.append(_ev(child, "end", state="failed", detail=err or "subagent failed"))
        return events
    return []


ADAPTERS: dict[str, Callable[[dict[str, Any]], list[dict[str, Any]]]] = {
    "claude-code": claude_code_events,
}


def _gateway_base() -> str | None:
    from navig_agentctl.spool import config_dir

    try:
        raw = json.loads((config_dir() / "gateway.json").read_text(encoding="utf-8"))
        port = int(raw.get("port") or 0)
        host = str(raw.get("host") or "127.0.0.1")
        if 0 < port < 65536:
            return f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}"
    except (OSError, ValueError):
        pass
    return None


def post(events: list[dict[str, Any]], *, timeout: float = POST_TIMEOUT_S) -> bool:
    """POST *events* to the local gateway. True only on a 2xx; never raises, never spools."""
    base = os.environ.get("NAVIG_AGENT_MONITOR_URL") or _gateway_base()
    if not base:
        return False
    try:
        req = urllib.request.Request(
            f"{base}/api/deck/agents/events",
            data=json.dumps({"events": events}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — loopback URL
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def deliver(events: list[dict[str, Any]]) -> bool:
    """POST *events*; on any failure spool them for the daemon to drain. Never raises."""
    if not events:
        return True
    if post(events):
        return True
    from navig_agentctl import spool

    return all([spool.append(e) for e in events])


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    agent = args[args.index("--agent") + 1] if "--agent" in args and args.index("--agent") + 1 < len(args) \
        else "claude-code"
    try:
        adapter = ADAPTERS.get(agent)
        payload = json.loads(sys.stdin.read() or "{}")
        if adapter and isinstance(payload, dict):
            deliver(adapter(payload))
    except Exception:  # noqa: BLE001 — fail-open by contract: never disturb the observed agent
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
