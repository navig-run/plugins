"""``navig agentctl run`` — run a command as a monitored, verified NAVIG process.

The wrapper owns the process, so unlike a hook it knows the real PID, the real
exit code and the real output. It records: start, last activity (output), stages
(land adapter), exit code, an optional ``--verify`` command, and a redacted log.

Delivery goes through the gateway when it is up (so the Dock, dashboard and
notifications see it live). When it is not, the same event is written straight to
the run ledger so the record still exists — a monitor must not depend on the
thing being monitored being observable.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from navig_agentctl import hook, land_adapter, logs
from navig_agentctl.redact import redact

ACTIVITY_EVERY_S = 3.0
VERIFY_TIMEOUT_S = 120
MAX_LOG_BYTES = 8 * 1024 * 1024  # stop writing (but keep running) past this


class _Emitter:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self._lock = threading.Lock()

    def __call__(self, etype: str, **fields: Any) -> None:
        ev = {"event_id": uuid.uuid4().hex, "run_id": self.run_id, "type": etype}
        ev.update({k: v for k, v in fields.items() if v is not None and v != ""})
        with self._lock:
            if hook.post([ev], timeout=1.5):
                return
            try:  # daemon down: record locally; the ledger is the source of truth
                from navig_agentctl.registry import get_agentctl_store

                ev.setdefault("node", _hostname())
                get_agentctl_store().apply_event(ev)
            except Exception:  # noqa: BLE001 — never let bookkeeping kill the command
                pass


def _hostname() -> str:
    import socket

    return socket.gethostname()


def _resolve_argv(argv: list[str]) -> list[str]:
    """Windows: `npm`/`pnpm` are .cmd shims that CreateProcess cannot launch directly."""
    exe = shutil.which(argv[0])
    if exe and sys.platform == "win32" and exe.lower().endswith((".cmd", ".bat")):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", exe, *argv[1:]]
    return [exe, *argv[1:]] if exe else argv


def _kill_tree(pid: int) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=15)  # noqa: S603,S607
        else:
            import signal

            os.killpg(os.getpgid(pid), signal.SIGTERM)
    except Exception:  # noqa: BLE001
        pass


def _create_time(pid: int) -> float | None:
    try:
        import psutil

        return psutil.Process(pid).create_time()
    except Exception:  # noqa: BLE001
        return None


def run_command(
    argv: list[str],
    *,
    run_id: str = "",
    label: str = "",
    project: str = "",
    cwd: Path | None = None,
    stall_after: int | None = None,
    verify_cmd: str = "",
    adapter: str = "",
) -> int:
    """Run *argv*, record everything, return the command's exit code (1 if verification failed)."""
    if not argv:
        raise ValueError("no command given")
    run_id = run_id or f"run:{uuid.uuid4().hex[:12]}"
    from navig.platform.paths import invocation_cwd

    cwd = cwd or invocation_cwd()  # "where the operator ran this", not wherever navig chdir'd to
    root = logs.log_root()
    root.mkdir(parents=True, exist_ok=True)
    stem = run_id.replace(":", "-")
    log_path = root / f"{stem}.log"
    emit = _Emitter(run_id)

    env = dict(os.environ)
    ctx: dict[str, str] = {}
    events_path: Path | None = None
    kind, planned = "navig", None
    if adapter == "land":
        kind, planned = "land", land_adapter.STAGES
        events_path = root / f"{stem}.events.jsonl"
        events_path.write_text("", encoding="utf-8")
        env["NAVIG_LAND_EVENTS"] = str(events_path)
        ctx = land_adapter.context(cwd)
    label = label or (f"land {ctx.get('branch', '')}".strip() if adapter == "land" else " ".join(argv)[:60])
    project = project or ctx.get("project") or cwd.name

    try:
        proc = subprocess.Popen(  # noqa: S603 — argv is the user's own command
            _resolve_argv(argv), cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
        )
    except OSError as exc:
        emit("start", kind=kind, label=label, project=project, command=" ".join(argv))
        emit("end", state="failed", exit_code=127, detail=f"could not start: {exc}")
        return 127

    emit("start", kind=kind, label=label, project=project, worktree=ctx.get("worktree") or str(cwd),
         command=" ".join(argv), pid=proc.pid, pid_create_time=_create_time(proc.pid),
         log_path=str(log_path), stall_after_s=stall_after, stages_planned=planned)

    land_state: dict[str, Any] = {"meta": {}}
    stop_watch = threading.Event()

    def watch_events() -> None:
        offset = 0
        while True:
            done = stop_watch.is_set()  # drain once more after the process ends
            lines, offset = land_adapter.read_new_lines(events_path, offset)  # type: ignore[arg-type]
            for obj in lines:
                meta = obj.get("meta") if isinstance(obj.get("meta"), dict) else {}
                land_state["meta"].update({k: v for k, v in meta.items() if isinstance(v, str)})
                if obj["stage"] != "result":
                    emit("stage", stage={"name": obj["stage"], "state": obj.get("state") or "done",
                                         "detail": obj.get("detail") or ""})
            if done:
                return
            time.sleep(0.4)

    watcher = None
    if events_path is not None:
        watcher = threading.Thread(target=watch_events, daemon=True)
        watcher.start()

    last_emit = 0.0
    written = 0
    interrupted = False
    try:
        with log_path.open("w", encoding="utf-8", errors="replace") as log:
            assert proc.stdout is not None
            for raw in iter(proc.stdout.readline, b""):
                line = redact(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
                if written < MAX_LOG_BYTES:
                    log.write(line + "\n")
                    log.flush()
                    written += len(line) + 1
                now = time.monotonic()
                if now - last_emit >= ACTIVITY_EVERY_S:
                    last_emit = now
                    emit("activity", detail=line[:200])
        rc = proc.wait()
    except KeyboardInterrupt:
        interrupted = True
        _kill_tree(proc.pid)
        rc = proc.wait()
    finally:
        stop_watch.set()
        if watcher is not None:
            watcher.join(timeout=3)

    if interrupted:
        emit("end", state="interrupted", detail="stopped by the operator (Ctrl+C)")
        return 130

    state, detail, verified = ("succeeded" if rc == 0 else "failed"), "", None
    if rc == 0 and adapter == "land":
        meta = land_state["meta"]
        outcome = meta.get("outcome", "")
        if outcome in ("nothing", "dry-run"):
            detail = "nothing to land" if outcome == "nothing" else "dry run — nothing landed"
        else:
            ok, why = land_adapter.verify(cwd, meta.get("branch") or ctx.get("branch", ""), meta.get("sha", ""))
            verified, detail = ok, why
            if not ok:
                state = "failed"
    if rc == 0 and verify_cmd and state == "succeeded":
        try:
            vp = subprocess.run(  # noqa: S602 — the operator's own verification command
                verify_cmd, shell=True, cwd=str(cwd), capture_output=True, text=True,
                timeout=VERIFY_TIMEOUT_S, encoding="utf-8", errors="replace",
            )
            verified = vp.returncode == 0
            detail = f"verify {'passed' if verified else f'failed (exit {vp.returncode})'}: {verify_cmd[:80]}"
        except (OSError, subprocess.TimeoutExpired) as exc:
            verified, detail = False, f"verify could not run: {exc}"
        if not verified:
            state = "failed"
    emit("end", state=state, exit_code=rc, verified=verified, detail=redact(detail, limit=300))
    return rc if state == "succeeded" or rc != 0 else 1


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if "--" not in args:
        print("usage: python -m navig_agentctl.runner [options] -- <command…>", file=sys.stderr)
        return 2
    split = args.index("--")
    ap = argparse.ArgumentParser(prog="navig_agentctl.runner")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--label", default="")
    ap.add_argument("--project", default="")
    ap.add_argument("--cwd", default="")
    ap.add_argument("--stall-after", type=int, default=None)
    ap.add_argument("--verify", default="")
    ap.add_argument("--adapter", default="", choices=["", "land"])
    opts = ap.parse_args(args[:split])
    from navig.platform.paths import resolve_user_path

    return run_command(
        args[split + 1:], run_id=opts.run_id, label=opts.label, project=opts.project,
        cwd=resolve_user_path(opts.cwd) if opts.cwd else None, stall_after=opts.stall_after,
        verify_cmd=opts.verify, adapter=opts.adapter,
    )


if __name__ == "__main__":
    sys.exit(main())
