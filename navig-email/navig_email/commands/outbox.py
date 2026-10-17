"""navig email outbox — every message the mailroom sends: review, approve, schedule, cancel.

Nothing here sends on its own. ``approve`` schedules the exact text you were shown (default
15 minutes later, so there is time to cancel); the daemon's mailroom tick sends it when due —
**while this computer is online**. ``send-now`` is the only verb that dispatches immediately,
and it still refuses an unapproved or changed text.
"""

from __future__ import annotations

import json as _json
from typing import Annotated, Any

import typer
from rich.table import Table

from navig_sdk import console as ch
from navig_sdk.host import command_name

CMD = command_name("email")

outbox_app = typer.Typer(
    help="📤 The outbox: queued replies — review, approve (default: send in 15 min), delay, cancel, send now.",
    no_args_is_help=True,
)

_STATE_STYLE = {
    "DRAFT": "[dim]○ draft[/dim]",
    "PENDING_APPROVAL": "[yellow]◐ awaiting approval[/yellow]",
    "SCHEDULED": "[cyan]◷ scheduled[/cyan]",
    "CLAIMED": "[cyan]◷ claimed[/cyan]",
    "SENDING": "[cyan]➜ sending[/cyan]",
    "SENT": "[green]● sent[/green]",
    "CANCELLED": "[dim]✗ cancelled[/dim]",
    "FAILED": "[red]✗ failed[/red]",
    "NEEDS_REVIEW": "[red]! needs review[/red]",
    "OUTCOME_UNKNOWN": "[yellow]? checking Sent[/yellow]",
}
_NEXT = {
    "DRAFT": "approve",
    "PENDING_APPROVAL": "approve",
    "SCHEDULED": "cancel · delay · send-now",
    "NEEDS_REVIEW": "show · reopen · cancel",
    "FAILED": "show · reopen",
    "OUTCOME_UNKNOWN": "wait (searching Sent)",
}


def _ob():
    from navig_email import outbox

    return outbox


def _print_json(payload: Any) -> None:
    ch.console.print_json(_json.dumps(payload, default=str))


def _fail(exc: Exception) -> None:
    ch.error(str(exc))
    raise typer.Exit(1) from exc


def _public(job: dict[str, Any], *, body: bool = False) -> dict[str, Any]:
    keep = ("job_id", "state", "origin", "policy_id", "provider", "account", "from_addr", "identity",
            "to_addrs", "cc_addrs", "subject", "thread_id", "not_before", "sent_at", "content_hash",
            "approved_by", "approved_at", "last_error", "attempts", "provider_msg_id", "style", "model",
            "created_at", "updated_at", "version")
    out = {k: job.get(k) for k in keep}
    if job.get("quality_json"):
        try:
            out["quality"] = _json.loads(job["quality_json"])
        except ValueError:
            pass
    if body:
        out["body"] = job.get("body")
    return out


def _when(job: dict[str, Any]) -> str:
    v = job.get("sent_at") if job.get("state") == "SENT" else job.get("not_before")
    return (v or "")[:16].replace("T", " ")


@outbox_app.command("list")
def cmd_list(
    state: Annotated[list[str] | None, typer.Option("--state", "-s", help="Filter by state (repeatable).")] = None,
    open_only: Annotated[bool, typer.Option("--open", help="Only jobs not yet sent or cancelled.")] = False,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 30,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Queued, sent and stuck messages, newest first."""
    from navig_email import account

    ob = _ob()
    states = [s.upper() for s in state] if state else (list(ob.OPEN) if open_only else None)
    rows = ob.list_jobs(states=states, account=account.CURRENT or "", limit=limit)
    if as_json:
        _print_json({"jobs": [_public(j) for j in rows], "pause": ob.pause_level()})
        return
    pause = ob.pause_level()
    if pause:
        ch.warning(f"Sending is PAUSED ({'automatic replies only' if pause == 'auto' else 'everything'}) — "
                   f"`{CMD} outbox resume`")
    if not rows:
        ch.info("The outbox is empty.")
        return
    t = Table(box=None, show_header=True, padding=(0, 2))
    t.add_column("Job", no_wrap=True)
    t.add_column("State", no_wrap=True)
    t.add_column("When (UTC)", no_wrap=True)
    t.add_column("To", no_wrap=True, max_width=32)
    t.add_column("Subject")
    t.add_column("Next step", no_wrap=True)
    for j in rows:
        t.add_row(j["job_id"][:8], _STATE_STYLE.get(j["state"], j["state"]), _when(j),
                  (j.get("to_addrs") or "")[:32], (j.get("subject") or "")[:60], _NEXT.get(j["state"], "—"))
    ch.console.print(t)
    ch.dim(f"Scheduled jobs send while this computer's navig daemon runs. Details: `{CMD} outbox show <job>`")


@outbox_app.command("show")
def cmd_show(
    job: Annotated[str, typer.Argument(help="Job id (or a unique prefix).")],
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """One job: the exact text, its approval, and its full history."""
    ob = _ob()
    try:
        row = ob.require(job)
    except ob.OutboxError as exc:
        _fail(exc)
    history = ob.events(row["job_id"])
    if as_json:
        _print_json({"job": _public(row, body=True), "events": history})
        return
    ch.console.rule(f"{row['job_id'][:8]} · {_STATE_STYLE.get(row['state'], row['state'])}")
    for label, key in (("From", "from_addr"), ("To", "to_addrs"), ("Cc", "cc_addrs"), ("Subject", "subject"),
                       ("Identity", "identity"), ("Origin", "origin"), ("Sends at (UTC)", "not_before"),
                       ("Error", "last_error")):
        if row.get(key):
            ch.console.print(f"  [bold]{label:<15}[/bold] {row[key]}")
    ch.console.print(f"  [bold]{'Version hash':<15}[/bold] {row['content_hash'][:12]}"
                     + ("  [green](approved)[/green]" if row.get("approved_hash") == row["content_hash"] else ""))
    if row.get("quality_json"):
        q = _json.loads(row["quality_json"])
        issues = q.get("issues") or []
        ch.console.print(f"  [bold]{'Quality':<15}[/bold] "
                         + ("[green]clean[/green]" if not issues else "; ".join(i["message"] for i in issues)))
    ch.console.rule()
    ch.console.print(row.get("body") or "")
    ch.console.rule()
    t = Table(box=None, show_header=True, padding=(0, 2))
    t.add_column("At (UTC)", no_wrap=True)
    t.add_column("Who", no_wrap=True)
    t.add_column("Change", no_wrap=True)
    t.add_column("Note")
    for e in history:
        t.add_row(e["at"][:19].replace("T", " "), e["actor"], f"{e['from_state'] or '·'} → {e['to_state']}", e["note"])
    ch.console.print(t)


@outbox_app.command("approve")
def cmd_approve(
    job: Annotated[str, typer.Argument(help="Job id (or a unique prefix).")],
    send_in: Annotated[str | None, typer.Option("--in", help="Delay: 5m, 15m, 30m, 1h… (default 15m).")] = None,
    at: Annotated[str | None, typer.Option("--at", help="An absolute time, e.g. 2026-10-20T09:00 (local).")] = None,
    version: Annotated[str | None, typer.Option("--version", help="The version hash you reviewed (from `show`).")] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Approve the CURRENT text without passing --version.")] = False,
) -> None:
    """Approve the text you reviewed and schedule it (default: in 15 minutes — time to cancel)."""
    ob = _ob()
    try:
        row = ob.require(job)
        if not (version or yes):
            ch.error(f"review it first (`{CMD} outbox show {row['job_id'][:8]}`), then pass --version "
                     f"{row['content_hash'][:12]} or --yes")
            raise typer.Exit(2)
        seen = row["content_hash"] if yes and not version else _full_hash(row, version or "")
        when = ob.parse_when(at) if at else (ob.parse_when(send_in) if send_in else None)
        row = ob.approve(row["job_id"], content_hash_seen=seen, send_at=when, actor="user:cli")
    except ob.OutboxError as exc:
        _fail(exc)
    ch.success(f"Approved {row['job_id'][:8]} — sends at {_when(row)} UTC (not sent yet).")
    ch.dim(f"Cancel: `{CMD} outbox cancel {row['job_id'][:8]}` · now: `{CMD} outbox send-now {row['job_id'][:8]} --yes`")


def _full_hash(row: dict[str, Any], prefix: str) -> str:
    return row["content_hash"] if prefix and row["content_hash"].startswith(prefix.lower()) else prefix


@outbox_app.command("edit")
def cmd_edit(
    job: Annotated[str, typer.Argument(help="Job id (or a unique prefix).")],
    body: Annotated[str | None, typer.Option("--body", "-b", help="New body text.")] = None,
    body_file: Annotated[str | None, typer.Option("--body-file", help="Read the new body from a file.")] = None,
    subject: Annotated[str | None, typer.Option("--subject", "-s")] = None,
    to: Annotated[str | None, typer.Option("--to")] = None,
) -> None:
    """Change the text. The approval is dropped — the new version must be approved again."""
    ob = _ob()
    if body_file:
        try:
            from navig.platform.paths import resolve_user_path
        except ImportError:  # standalone: no invocation-cwd tracking to honour
            from pathlib import Path

            def resolve_user_path(p: str) -> Path:  # type: ignore[misc]
                return Path(p).expanduser()

        body = resolve_user_path(body_file).read_text(encoding="utf-8")
    try:
        row = ob.edit(ob.require(job)["job_id"], body=body, subject=subject, to=to, actor="user:cli")
    except ob.OutboxError as exc:
        _fail(exc)
    ch.success(f"Updated {row['job_id'][:8]} — {row['state'].lower().replace('_', ' ')}; approve the new version "
               f"({row['content_hash'][:12]}) to send it.")


@outbox_app.command("cancel")
def cmd_cancel(job: Annotated[str, typer.Argument(help="Job id (or a unique prefix).")]) -> None:
    """Cancel a queued message. A cancelled job can never be sent."""
    ob = _ob()
    try:
        row = ob.cancel(ob.require(job)["job_id"], actor="user:cli")
    except ob.OutboxError as exc:
        _fail(exc)
    ch.success(f"Cancelled {row['job_id'][:8]} — it will not be sent.")


@outbox_app.command("delay")
def cmd_delay(
    job: Annotated[str, typer.Argument(help="Job id (or a unique prefix).")],
    by: Annotated[str | None, typer.Option("--by", help="Push back by 15m, 1h, 1d…")] = None,
    at: Annotated[str | None, typer.Option("--at", help="Or move to this time (local).")] = None,
) -> None:
    """Move a scheduled send later (or to a given time)."""
    ob = _ob()
    try:
        row = ob.require(job)
        if at:
            when = ob.parse_when(at)
        elif by:
            when = max(ob.parse_iso(row.get("not_before")) or ob.now_utc(), ob.now_utc()) + ob.parse_delay(by)
        else:
            ch.error("pass --by or --at")
            raise typer.Exit(2)
        row = ob.reschedule(row["job_id"], when, actor="user:cli")
    except ob.OutboxError as exc:
        _fail(exc)
    ch.success(f"{row['job_id'][:8]} now sends at {_when(row)} UTC.")


@outbox_app.command("send-now")
def cmd_send_now(
    job: Annotated[str, typer.Argument(help="Job id (or a unique prefix).")],
    yes: Annotated[bool, typer.Option("--yes", help="Required: this sends mail.")] = False,
) -> None:
    """Send an approved, scheduled message right now (skips the rest of the wait)."""
    from navig_email import account, mail_notify

    ob = _ob()
    if not yes:
        ch.error("this sends mail: add --yes")
        raise typer.Exit(2)
    try:
        row = ob.reschedule(ob.require(job)["job_id"], ob.now_utc(), actor="user:cli")

        async def _go():
            stats = await ob.tick(only=row["job_id"])
            await mail_notify.drain()
            return stats

        account.run(_go())
        row = ob.require(row["job_id"])
    except ob.OutboxError as exc:
        _fail(exc)
    _report_outcome(row)


def _report_outcome(row: dict[str, Any]) -> None:
    state = row["state"]
    if state == "SENT":
        ch.success(f"Sent to {row['to_addrs']} (job {row['job_id'][:8]}, confirmed by the provider).")
    elif state == "OUTCOME_UNKNOWN":
        ch.warning(f"{row['job_id'][:8]}: the provider did not answer — it MAY have gone out. "
                   "It will not be resent; Sent is being checked.")
        raise typer.Exit(1)
    elif state in ("FAILED", "NEEDS_REVIEW"):
        ch.error(f"{row['job_id'][:8]} not sent: {row.get('last_error') or state}")
        raise typer.Exit(1)
    else:
        ch.info(f"{row['job_id'][:8]} is {state.lower().replace('_', ' ')}.")


@outbox_app.command("reopen")
def cmd_reopen(job: Annotated[str, typer.Argument(help="Job id (or a unique prefix).")]) -> None:
    """Put a failed or reviewed job back in front of you for approval (never straight to sending)."""
    ob = _ob()
    try:
        row = ob.reopen(ob.require(job)["job_id"], actor="user:cli")
    except ob.OutboxError as exc:
        _fail(exc)
    ch.success(f"{row['job_id'][:8]} is awaiting approval again.")


@outbox_app.command("confirm-sent")
def cmd_confirm_sent(job: Annotated[str, typer.Argument(help="Job id (or a unique prefix).")]) -> None:
    """You found a NEEDS_REVIEW message in Sent: record that it did go out."""
    ob = _ob()
    try:
        row = ob.mark_sent_manually(ob.require(job)["job_id"], actor="user:cli")
    except ob.OutboxError as exc:
        _fail(exc)
    ch.success(f"{row['job_id'][:8]} recorded as sent.")


@outbox_app.command("dispatch")
def cmd_dispatch(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Run one dispatcher pass now (the daemon does this every ~45 s)."""
    from navig_email import account, mail_notify

    ob = _ob()

    async def _go():
        stats = await ob.tick()
        await mail_notify.drain()
        return stats

    stats = account.run(_go())
    if as_json:
        _print_json(stats)
        return
    ch.info(" · ".join(f"{k} {v}" for k, v in stats.items()))


@outbox_app.command("pause")
def cmd_pause(
    everything: Annotated[bool, typer.Option("--all", help="Pause every send, not only automatic replies.")] = False,
) -> None:
    """Emergency stop: hold automatic replies (--all: every send) until `resume`."""
    ob = _ob()
    ob.set_pause("all" if everything else "auto", actor="user:cli")
    ch.warning("Sending paused: " + ("everything." if everything else "automatic replies.")
               + f" Queued jobs stay visible and cancellable. Resume: `{CMD} outbox resume`")


@outbox_app.command("resume")
def cmd_resume() -> None:
    """Lift the pause."""
    _ob().set_pause("", actor="user:cli")
    ch.success("Sending resumed.")


@outbox_app.command("history")
def cmd_history(
    limit: Annotated[int, typer.Option("--limit", "-n")] = 40,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """The mailroom audit trail: who did what, when (no message bodies)."""
    from navig_email import account
    from navig_email.store import get_store

    rows = get_store().audit_rows(limit, account=account.CURRENT or "")
    if as_json:
        _print_json({"audit": rows})
        return
    if not rows:
        ch.info("No mailroom actions recorded yet.")
        return
    t = Table(box=None, show_header=True, padding=(0, 2))
    t.add_column("At (UTC)", no_wrap=True)
    t.add_column("Who", no_wrap=True)
    t.add_column("Action", no_wrap=True)
    t.add_column("Ref", no_wrap=True)
    t.add_column("Summary")
    for r in rows:
        t.add_row(r["at"][:19].replace("T", " "), r["actor"], r["action"], (r["ref"] or "")[:8], r["summary"])
    ch.console.print(t)
