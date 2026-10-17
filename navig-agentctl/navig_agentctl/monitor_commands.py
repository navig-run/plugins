"""``navig agentctl runs | run | show | ack | hooks`` — the Agent Monitor from the terminal.

Registered onto ``agentctl_app`` by ``commands.py``. Reads go straight to the run
ledger (a shared SQLite file), so they work with the daemon down; ``run`` and the
hook adapter feed the same ledger.
"""

from __future__ import annotations

import difflib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import typer

HOOK_MARKER = "navig_agentctl.hook"
#: Claude Code hook events the monitor listens to (documented events only).
TOOL_EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure")
OTHER_EVENTS = ("SessionStart", "SessionEnd", "UserPromptSubmit", "Stop", "SubagentStop",
                "TaskCompleted", "Notification")

hooks_app = typer.Typer(name="hooks", help="Wire the Agent Monitor into Claude Code (hooks).", no_args_is_help=True)


def _print_json(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, default=str))


def _state_glyph(state: str, verified: bool | None = None) -> str:
    return {
        "running": "[green]● running[/green]",
        "stalled": "[yellow]◐ stalled[/yellow]",
        "needs_input": "[magenta]! needs you[/magenta]",
        "launched": "[dim]◌ launched[/dim]",
        "succeeded": "[green]✓ verified[/green]" if verified else "[green]✓ succeeded[/green]",
        "failed": "[red]✗ failed[/red]",
        "interrupted": "[red]✗ interrupted[/red]",
        "ended": "[dim]○ ended[/dim]",
    }.get(state, f"[dim]? {state}[/dim]")


def _dur(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s" if m else f"{s}s"


def _progress(run: dict[str, Any]) -> str:
    """Declared stages only — never a percentage, never an estimate."""
    if run["stages_total"]:
        return f"{run['stages_done']}/{run['stages_total']} stages"
    return f"{run['stages_done']} done" if run["stages_done"] else "—"


def _store():
    from navig_agentctl.registry import get_agentctl_store

    return get_agentctl_store()


def register(app: typer.Typer) -> None:
    app.add_typer(hooks_app, name="hooks")

    @app.command("runs")
    def runs_cmd(
        active: bool = typer.Option(False, "--active", "-a", help="Only runs that are still live."),
        project: str = typer.Option("", "--project", "-p", help="Filter by project substring."),
        state: str = typer.Option("", "--state", help="running|stalled|needs_input|launched|succeeded|failed|interrupted|ended"),
        kind: str = typer.Option("", "--kind", help="session|subagent|bash|navig|land"),
        limit: int = typer.Option(30, "--limit", "-n", help="Max rows."),
        json_out: bool = typer.Option(False, "--json", help="Output as JSON."),
    ):
        """Agent runs: sessions, subagents, commands and lands, live and historical."""
        from navig.console_helper import console

        store = _store()
        store.sweep_runs()  # mark vanished/stalled runs before showing them
        rows = store.list_runs(project=project, state=state, kind=kind, active_only=active, limit=limit)
        if json_out:
            _print_json(rows)
            return
        if not rows:
            typer.echo("No runs recorded yet.")
            typer.echo("Next: navig agentctl run -- <command>   ·   navig agentctl hooks install")
            return
        _table(
            ["State", "Kind", "Run", "Project", "Elapsed", "Idle", "Progress", "Exit"],
            [
                [_state_glyph(r["state"], r["verified"]), r["kind"], r["label"][:42], r["project"][:18],
                 _dur(r["elapsed_s"]), _dur(r["idle_s"]), _progress(r),
                 "—" if r["exit_code"] is None else str(r["exit_code"])]
                for r in rows
            ],
            no_wrap={0, 1, 4, 5, 6, 7},
        )
        live = sum(1 for r in rows if r["state"] in ("running", "stalled", "needs_input", "launched"))
        failed = [r for r in rows if r["state"] == "failed" and not r["acked"]]
        gone = [r for r in rows if r["state"] == "interrupted" and not r["acked"]]
        parts = [f"{len(rows)} run(s)", f"{live} live", f"{len(failed)} failed"]
        if gone:
            # a run that stopped without reporting is not a failure — name it truthfully
            parts.append(f"{len(gone)} interrupted")
        console.print("\n" + ", ".join(parts) + ".")
        focus = failed or gone
        hint = f"navig agentctl show {focus[0]['run_id']} --log" if focus else "navig agentctl show <run-id> --log"
        typer.echo(f"Next: {hint}")

    @app.command("show")
    def show_cmd(
        run_id: str = typer.Argument(..., help="Run id (see `navig agentctl runs --json`)."),
        log: bool = typer.Option(False, "--log", help="Include the redacted log tail."),
        json_out: bool = typer.Option(False, "--json", help="Output as JSON."),
    ):
        """One run in detail: state, stages, history, and optionally its log tail."""
        from navig.console_helper import console
        from navig_agentctl import logs

        store = _store()
        raw = store.get_run_raw(run_id)
        if raw is None:
            typer.echo(f"No such run: {run_id}")
            raise typer.Exit(1)
        run = store.get_run(run_id)
        body = {"run": run, "events": store.run_events(run_id), "log": logs.tail(raw["log_path"]) if log else None}
        if json_out:
            _print_json(body)
            return
        console.print(f"{_state_glyph(run['state'], run['verified'])}  [bold]{run['label']}[/bold]  "
                      f"[dim]{run['kind']} · {run['project'] or '—'} · {run['node'] or 'this node'}[/dim]")
        console.print(f"  elapsed {_dur(run['elapsed_s'])} · idle {_dur(run['idle_s'])} · exit "
                      f"{'—' if run['exit_code'] is None else run['exit_code']} · {_progress(run)}")
        if run["detail"]:
            console.print(f"  {run['detail']}")
        for s in run["stages"]:
            console.print(f"    {'✓' if s['state'] == 'done' else '✗' if s['state'] == 'failed' else '·'} {s['name']}"
                          f"  [dim]{s['state']}[/dim]")
        if log:
            console.print("\n[dim]── log tail (redacted) ──[/dim]")
            for line in body["log"] or ["(no log for this run)"]:
                typer.echo(line)
        typer.echo(f"\nNext: navig agentctl ack {run_id}" if run["state"] in ("failed", "interrupted") and not run["acked"]
                   else "\nNext: navig agentctl runs --active")

    @app.command("ack")
    def ack_cmd(
        run_id: str = typer.Argument("", help="Run id, or omit with --all."),
        all_: bool = typer.Option(False, "--all", help="Acknowledge every unacknowledged failure."),
    ):
        """Acknowledge a failed run so it stops lighting the Dock indicator."""
        store = _store()
        ids = ([r["run_id"] for r in store.list_runs(state="failed", limit=500) + store.list_runs(state="interrupted", limit=500)
                if not r["acked"]] if all_ else [run_id])
        if not ids or ids == [""]:
            typer.echo("Give a run id or --all.")
            raise typer.Exit(1)
        n = sum(1 for i in ids if store.ack_run(i))
        typer.echo(f"Acknowledged {n} run(s).")

    @app.command("run", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
    def run_cmd(
        ctx: typer.Context,
        label: str = typer.Option("", "--label", "-l", help="Display name for the run."),
        project: str = typer.Option("", "--project", "-p", help="Project label (default: the checkout's name)."),
        cwd: str = typer.Option("", "--cwd", help="Working directory (default: where you ran this)."),
        stall_after: int = typer.Option(-1, "--stall-after", help="Seconds of silence before a stall alert; 0 disables."),
        verify: str = typer.Option("", "--verify", help="Command that must also exit 0 for the run to count as succeeded."),
        adapter: str = typer.Option("", "--adapter", help="'land' reads npm-run-land milestones and verifies the result in git."),
        detach: bool = typer.Option(False, "--detach", help="Start in the background and return the run id."),
    ):
        """Run a command as a monitored, verified process:  navig agentctl run -- <command…>"""
        from navig.platform.paths import invocation_cwd, resolve_user_path
        from navig_agentctl import runner

        argv = list(ctx.args)
        if not argv:
            typer.echo("Give a command after --, e.g.  navig agentctl run -- npm run land")
            raise typer.Exit(2)
        if adapter not in ("", "land"):
            typer.echo("--adapter must be 'land' (or omitted)")
            raise typer.Exit(2)
        if adapter == "" and argv[:3] == ["npm", "run", "land"]:
            adapter = "land"  # the one command with a known milestone contract
        workdir = resolve_user_path(cwd) if cwd else invocation_cwd()
        import uuid

        run_id = f"run:{uuid.uuid4().hex[:12]}"
        opts = ["--run-id", run_id, "--cwd", str(workdir)]
        for flag, val in (("--label", label), ("--project", project), ("--verify", verify), ("--adapter", adapter)):
            if val:
                opts += [flag, val]
        if stall_after >= 0:
            opts += ["--stall-after", str(stall_after)]
        if detach:
            flags = 0
            if sys.platform == "win32":
                flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            subprocess.Popen(  # noqa: S603 — our own module, operator's argv
                [sys.executable, "-m", "navig_agentctl.runner", *opts, "--", *argv],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=flags, close_fds=True,
            )
            typer.echo(f"Started {run_id}")
            typer.echo(f"Next: navig agentctl show {run_id} --log")
            return
        typer.echo(f"▶ {run_id}  (watch: navig agentctl runs --active)")
        rc = runner.run_command(
            argv, run_id=run_id, label=label, project=project, cwd=workdir,
            stall_after=None if stall_after < 0 else stall_after, verify_cmd=verify, adapter=adapter,
        )
        run = _store().get_run(run_id)
        if run:
            typer.echo(f"{'✓' if run['state'] == 'succeeded' else '✗'} {run['state']} · exit {run['exit_code']} · "
                       f"{_dur(run['elapsed_s'])}" + (f" · {run['detail']}" if run["detail"] else ""))
        raise typer.Exit(rc)

    # ── hooks ──────────────────────────────────────────────────────────────

    @hooks_app.command("install")
    def hooks_install(
        scope: str = typer.Option("user", "--scope", help="user = ~/.claude/settings.json (all projects) · local = this repo's .claude/settings.local.json"),
        yes: bool = typer.Option(False, "--yes", help="Write the change. Without it, only the diff is shown."),
    ):
        """Add the Agent Monitor hooks to Claude Code settings (shows the diff first)."""
        _apply_hooks(scope, install=True, yes=yes)

    @hooks_app.command("uninstall")
    def hooks_uninstall(
        scope: str = typer.Option("user", "--scope"),
        yes: bool = typer.Option(False, "--yes", help="Write the change. Without it, only the diff is shown."),
    ):
        """Remove only the Agent Monitor hooks; every other hook is left as it was."""
        _apply_hooks(scope, install=False, yes=yes)

    @hooks_app.command("status")
    def hooks_status():
        """Which Claude Code settings files carry the monitor hooks."""
        for scope in ("user", "local"):
            path = _settings_path(scope)
            data = _read_settings(path)
            have = [e for e in (*TOOL_EVENTS, *OTHER_EVENTS) if _has_marker(data.get("hooks", {}).get(e, []))]
            mark = "[green]●[/green]" if len(have) == len(TOOL_EVENTS) + len(OTHER_EVENTS) else "[yellow]◐[/yellow]" if have else "[dim]○[/dim]"
            from navig.console_helper import console

            console.print(f"{mark} {scope:<6} {path}  [dim]{len(have)}/{len(TOOL_EVENTS) + len(OTHER_EVENTS)} events[/dim]")
        typer.echo("Hooks load when a Claude Code session STARTS — running sessions are not affected.")
        typer.echo("Next: navig agentctl hooks install --scope user  (shows the diff; add --yes to write)")


def _table(headers: list[str], rows: list[list[str]], no_wrap: set[int] | None = None) -> None:
    from navig.console_helper import Table, console

    table = Table(box=None, show_header=True, padding=(0, 2))
    for i, h in enumerate(headers):
        table.add_column(h, no_wrap=i in (no_wrap or set()))
    for row in rows:
        table.add_row(*row)
    console.print(table)


# ── Claude Code settings editing ───────────────────────────────────────────────

def _settings_path(scope: str) -> Path:
    if scope == "user":
        return Path.home() / ".claude" / "settings.json"
    if scope == "local":
        from navig.platform.paths import invocation_cwd

        cwd = invocation_cwd()
        rc = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace")  # noqa: S603,S607
        root = Path(rc.stdout.strip()) if rc.returncode == 0 and rc.stdout.strip() else cwd
        return root / ".claude" / "settings.local.json"
    typer.echo("--scope must be 'user' or 'local'")
    raise typer.Exit(2)


def _read_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        typer.echo(f"Refusing to touch {path}: it is not valid JSON ({exc}).")
        raise typer.Exit(1) from exc
    if not isinstance(data, dict):
        typer.echo(f"Refusing to touch {path}: top level is not an object.")
        raise typer.Exit(1)
    return data


def _has_marker(groups: Any) -> bool:
    return any(HOOK_MARKER in str(h.get("command", "")) for g in groups or [] if isinstance(g, dict)
               for h in g.get("hooks", []) if isinstance(h, dict))


def hook_command() -> str:
    # Forward slashes: Claude Code runs hook commands through a POSIX-ish shell on Windows,
    # where backslashes in a quoted path would be eaten.
    return f'"{Path(sys.executable).as_posix()}" -I -m {HOOK_MARKER} --agent claude-code'


def desired_settings(data: dict[str, Any], *, install: bool) -> dict[str, Any]:
    """*data* with the monitor hooks added (or, for uninstall, removed). Other hooks are untouched."""
    out = json.loads(json.dumps(data))
    hooks = out.setdefault("hooks", {}) if install else out.get("hooks", {})
    cmd = hook_command()
    for event in (*TOOL_EVENTS, *OTHER_EVENTS):
        groups = [g for g in hooks.get(event, []) if isinstance(g, dict)]
        kept = []
        for g in groups:
            rest = [h for h in g.get("hooks", []) if not (isinstance(h, dict) and HOOK_MARKER in str(h.get("command", "")))]
            if rest:
                kept.append({**g, "hooks": rest})
        if install:
            group: dict[str, Any] = {"hooks": [{"type": "command", "command": cmd, "timeout": 5}]}
            if event in TOOL_EVENTS:
                group = {"matcher": "*", **group}
            kept.append(group)
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    if not install and not hooks:
        out.pop("hooks", None)
    return out


def _apply_hooks(scope: str, *, install: bool, yes: bool) -> None:
    path = _settings_path(scope)
    before = _read_settings(path)
    after = desired_settings(before, install=install)
    a, b = json.dumps(before, indent=2, sort_keys=False), json.dumps(after, indent=2, sort_keys=False)
    if a == b:
        typer.echo(f"{path}: already {'installed' if install else 'clean'} — nothing to change.")
        return
    diff = "\n".join(difflib.unified_diff(a.splitlines(), b.splitlines(), f"{path} (current)", f"{path} (proposed)", lineterm=""))
    typer.echo(diff)
    if not yes:
        typer.echo("\nDry run — nothing written. Re-run with --yes to apply.")
        typer.echo("Hooks load when a Claude Code session starts; sessions already running are not affected.")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_suffix(path.suffix + ".navig-bak")
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(b + "\n", encoding="utf-8")
    os.replace(tmp, path)
    typer.echo(f"\n{'Installed into' if install else 'Removed from'} {path}")
    typer.echo("Next: start a new Claude Code session, then  navig agentctl runs --active")
