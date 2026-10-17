"""``navig agentctl`` — the Agent Control Center from the terminal.

Read-only Observer verbs mirroring the deck routes. Tables follow the house
style (Rich, one wrappable column, semantic glyphs); anything a script might
parse also offers ``--json``.
"""

from __future__ import annotations

import json
from typing import Any

import typer

agentctl_app = typer.Typer(
    name="agentctl",
    help="Agent Control Center: sessions, worktrees, tasks & the Agent Monitor (runs, hooks).",
    no_args_is_help=True,
)


def _print_json(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, default=str))


def _table(headers: list[str], rows: list[list[str]], no_wrap: set[int] | None = None):
    from navig.console_helper import Table, console

    table = Table(box=None, show_header=True, padding=(0, 2))
    nw = no_wrap or set()
    for i, h in enumerate(headers):
        table.add_column(h, no_wrap=i in nw)
    for row in rows:
        table.add_row(*row)
    console.print(table)


def _state_glyph(state: str) -> str:
    return {
        "active": "[green]● active[/green]",
        "idle": "[dim]○ idle[/dim]",
        "young": "[cyan]◐ young[/cyan]",
        "stale": "[red]✗ stale[/red]",
    }.get(state, f"[dim]? {state or 'unknown'}[/dim]")


@agentctl_app.command("sessions")
def sessions_cmd(
    project: str = typer.Option("", "--project", "-p", help="Filter by project label substring."),
    json_out: bool = typer.Option(False, "--json", help="Output as JSON."),
):
    """Live agent sessions (who is working where, idle or active)."""
    from navig_agentctl.snapshots import session_snapshot

    rows = session_snapshot()
    if project:
        rows = [r for r in rows if project.lower() in (r.get("project") or "").lower()]
    if json_out:
        _print_json(rows)
        return
    if not rows:
        typer.echo("No agent sessions found.")
        return
    _table(
        ["State", "Session", "Project", "Owner", "Mem", "Reason"],
        [
            [
                _state_glyph(r["state"]),
                (r["session_id"] or "")[:12],
                (r.get("project") or "")[:28],
                (r.get("owner") or "")[:16],
                f"{r.get('mem_mb') or 0:.0f}MB",
                (r.get("reason") or "")[:40],
            ]
            for r in rows
        ],
        no_wrap={0, 1, 4},
    )
    typer.echo(f"\n{len(rows)} session(s). Next: navig agentctl worktrees --project <path>")


@agentctl_app.command("worktrees")
def worktrees_cmd(
    project: str = typer.Option(".", "--project", "-p", help="Repo path (default: current directory)."),
    conflicts: bool = typer.Option(False, "--conflicts", help="Also run the pairwise conflict radar."),
    json_out: bool = typer.Option(False, "--json", help="Output as JSON."),
):
    """Git worktrees of a repo: branch, ahead/behind, dirty, last commit."""
    from navig_agentctl.snapshots import conflict_pairs, worktree_snapshot

    # Typed paths follow the operator: resolve against the invocation cwd, never
    # the process cwd (navig chdirs into the active space).
    from navig.platform.paths import resolve_user_path

    root = resolve_user_path(project)
    if not root.exists():
        typer.echo(f"Path not found: {root}")
        raise typer.Exit(1)
    wts = worktree_snapshot(root)
    pairs = conflict_pairs(root) if conflicts else []
    if json_out:
        _print_json({"project": str(root), "worktrees": wts, "conflicts": pairs})
        return
    if not wts:
        typer.echo(f"No worktrees found under {root}")
        return
    _table(
        ["Worktree", "Branch", "±", "Dirty", "Last commit"],
        [
            [
                w["name"][:28],
                (w["branch"] or "detached")[:24],
                f"+{w['ahead']}/-{w['behind']}",
                "[yellow]●[/yellow]" if w["dirty"] else "[dim]○[/dim]",
                (w["last_commit"]["subject"] or "")[:36],
            ]
            for w in wts
        ],
        no_wrap={2, 3},
    )
    if conflicts:
        if not pairs:
            typer.echo("\n[green]No merge conflicts across worktrees.[/green]")
        else:
            _table(
                ["A", "B", "Status"],
                [
                    [p.get("a", "")[:18], p.get("b", "")[:18], p.get("status", "")[:20]]
                    for p in pairs
                ],
                no_wrap={2},
            )
    typer.echo(f"\n{len(wts)} worktree(s). Next: navig agentctl tasks")


@agentctl_app.command("tasks")
def tasks_cmd(
    source: str = typer.Option("", "--source", "-s", help="Filter: board | work."),
    json_out: bool = typer.Option(False, "--json", help="Output as JSON."),
):
    """Unified tasks from the board and the work queue."""
    from navig_agentctl.snapshots import task_snapshot

    rows = task_snapshot()
    if source:
        rows = [r for r in rows if r.get("source") == source]
    if json_out:
        _print_json(rows)
        return
    if not rows:
        typer.echo("No task records found.")
        return
    _table(
        ["Ref", "Source", "Status", "Title", "Owner"],
        [
            [
                r["ref"][:26],
                r["source"],
                (r["status"] or "")[:10],
                (r["title"] or "")[:40],
                (r["owner"] or "")[:16],
            ]
            for r in rows
        ],
        no_wrap={0, 1, 2},
    )
    typer.echo(f"\n{len(rows)} task(s). Next: navig agentctl sessions")


def _binding_count() -> int:
    """Active task claims for the status overview (store down → 0, never raise)."""
    try:
        from navig_agentctl.registry import get_agentctl_store

        return len(get_agentctl_store().bindings(active_only=True))
    except Exception:  # noqa: BLE001 — status must not die over the ledger
        return 0


@agentctl_app.command("status")
def status_cmd(
    json_out: bool = typer.Option(False, "--json", help="Output as JSON."),
):
    """One-line overview: sessions, worktrees (cwd repo), tasks."""
    from navig_agentctl.snapshots import session_snapshot, task_snapshot, worktree_snapshot

    from navig.platform.paths import invocation_cwd

    sessions = session_snapshot()
    tasks = task_snapshot()
    wts = worktree_snapshot(invocation_cwd())
    active = sum(1 for s in sessions if s["state"] == "active")
    blocked = sum(1 for t in tasks if t.get("gate") == "waiting")
    summary = {
        "sessions_total": len(sessions),
        "sessions_active": active,
        "worktrees": len(wts),
        "tasks_total": len(tasks),
        "tasks_waiting": blocked,
        "bindings_active": _binding_count(),
    }
    if json_out:
        _print_json(summary)
        return
    _table(
        ["Metric", "Value"],
        [
            ["Sessions (active / total)", f"{active} / {len(sessions)}"],
            ["Worktrees (this repo)", str(len(wts))],
            ["Tasks (waiting / total)", f"{blocked} / {len(tasks)}"],
            ["Active task claims", str(summary["bindings_active"])],
        ],
        no_wrap={1},
    )
    typer.echo("\nNext: navig agentctl sessions  ·  worktrees --project <path>  ·  tasks")


@agentctl_app.command("bindings")
def bindings_cmd(
    all_rows: bool = typer.Option(False, "--all", help="Include released bindings (the ledger)."),
    json_out: bool = typer.Option(False, "--json", help="Output as JSON."),
):
    """Active task <-> session claims (one claim per task — no duplicate execution)."""
    from navig_agentctl.registry import get_agentctl_store

    rows = get_agentctl_store().bindings(active_only=not all_rows)
    if json_out:
        _print_json(rows)
        return
    if not rows:
        typer.echo("No active task claims." if not all_rows else "No binding history.")
        typer.echo("Claim one: navig agentctl bind board:<id> --session <session-id>")
        return
    _table(
        ["Task", "Session", "Bound", "Released", "Note"],
        [
            [
                r["task_ref"][:34],
                r["session_id"][:24],
                (r["bound_at"] or "")[:19],
                (r["released_at"] or "")[:19] if all_rows else "",
                (r.get("note") or "")[:28],
            ]
            for r in rows
        ],
        no_wrap={0},
    )
    typer.echo(
        f"{len(rows)} claim(s). Next: navig agentctl sessions (who holds it) "
        "· unbind <task-ref> to release"
    )


@agentctl_app.command("bind")
def bind_cmd(
    task_ref: str = typer.Argument(..., help="board:<id> or work:<slug>"),
    session: str = typer.Option(..., "--session", "-s", help="Session id holding the task."),
    note: str = typer.Option("", "--note", help="Optional free-text note (max 300 chars)."),
    json_out: bool = typer.Option(False, "--json", help="Output as JSON."),
):
    """Claim a task for a session. Rebinding MOVES the owner (one claim per task)."""
    import re as _re

    from navig.console_helper import console
    from navig_agentctl.registry import get_agentctl_store

    if not _re.fullmatch(r"(?:board|work):[A-Za-z0-9._-]{1,120}", task_ref):
        typer.echo("task_ref must look like 'board:<id>' or 'work:<slug>'")
        raise typer.Exit(1)
    if not session.strip() or any(ord(c) < 32 for c in session):
        typer.echo("session must be a non-empty id without control characters")
        raise typer.Exit(1)
    if len(note) > 300:
        typer.echo("note must be at most 300 chars")
        raise typer.Exit(1)
    store = get_agentctl_store()
    store.bind(task_ref, session.strip(), note=note)
    binding = store.binding_for(task_ref)
    if json_out:
        _print_json(binding)
        return
    console.print(f"[green]✓[/green] claimed {task_ref} for {session.strip()}")
    typer.echo("Next: navig agentctl bindings  · unbind to release")


@agentctl_app.command("unbind")
def unbind_cmd(
    task_ref: str = typer.Argument(..., help="board:<id> or work:<slug>"),
    json_out: bool = typer.Option(False, "--json", help="Output as JSON."),
):
    """Release a task claim (the ledger keeps the history)."""
    import re as _re

    from navig.console_helper import console
    from navig_agentctl.registry import get_agentctl_store

    if not _re.fullmatch(r"(?:board|work):[A-Za-z0-9._-]{1,120}", task_ref):
        typer.echo("task_ref must look like 'board:<id>' or 'work:<slug>'")
        raise typer.Exit(1)
    store = get_agentctl_store()
    was = store.binding_for(task_ref) is not None
    store.release(task_ref)
    if json_out:
        _print_json({"ok": True, "released": was, "task_ref": task_ref})
        return
    if was:
        console.print(f"[green]✓[/green] released {task_ref}")
    else:
        console.print(f"[yellow]○[/yellow] no active claim on {task_ref}")
    typer.echo("Next: navig agentctl bindings --all (ledger)")


# Agent Monitor verbs: runs · show · ack · run · hooks
from navig_agentctl import monitor_commands  # noqa: E402

monitor_commands.register(agentctl_app)
