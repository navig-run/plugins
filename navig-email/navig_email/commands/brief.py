"""navig email brief — the newsletter briefing: products, events and jobs, extracted and merged.

``run`` builds one now (``--dry-run`` shows it, sends nothing); ``run --scheduled`` is what the
cron job calls — it decides itself whether a briefing is due, postpones a thin one, and only
after a verified Telegram delivery archives the newsletters it used. ``schedule --apply``
moves the old weekly template digest over to it (the old job is disabled, not deleted).
"""

from __future__ import annotations

import json as _json
from typing import Annotated, Any

import typer
from rich.table import Table

from navig_sdk import console as ch
from navig_sdk.host import command_name

CMD = command_name("email")
_SPACE_HELP = "Space name or absolute path (holds mailroom/ and .navig/)."

brief_app = typer.Typer(
    help="📰 Newsletter briefing: extract products / events / jobs, merge across emails, deliver, tidy up.",
    no_args_is_help=True,
)

OLD_JOB = "mail:digest-weekly"
NEW_JOB = "mail:brief"


def _paths(space: str):
    from navig_email.space import paths_for

    try:
        return paths_for(space)
    except ValueError as exc:
        ch.error(str(exc))
        raise typer.Exit(2) from exc


def _print_json(payload: Any) -> None:
    ch.console.print_json(_json.dumps(payload, default=str, ensure_ascii=False))


@brief_app.command("run")
def cmd_run(
    space: Annotated[str, typer.Option("--space", help=_SPACE_HELP)],
    scheduled: Annotated[bool, typer.Option("--scheduled", help="Only if a briefing is due (what cron runs).")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Build and show it; send nothing, change nothing.")] = False,
    query: Annotated[str | None, typer.Option("--query", "-q", help="Gmail query for the sources (default: recent newsletters).")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Build the briefing and deliver it to Telegram (then archive the newsletters it used)."""
    from navig_email import account, mail_notify
    from navig_email.brief import run as RUN

    paths = _paths(space)

    async def _go():
        c = await account.connector()
        me = account.connected_email() or (await account.profile(c))["email"]
        res = await RUN.run(paths, connector=c, account=me, scheduled=scheduled, dry_run=dry_run, query=query,
                            allow_cloud=paths.ai_allow_cloud)
        await mail_notify.drain()
        return res

    try:
        res = account.run(_go())
    except account.NotConnected as exc:
        ch.error(str(exc))
        raise typer.Exit(1) from exc
    except ValueError as exc:  # a bad mailroom.brief config
        ch.error(str(exc))
        raise typer.Exit(2) from exc

    payload = {
        "status": res.status, "reason": res.reason, "run_id": res.run_id, "items": res.item_count,
        "sources": [{"id": m["id"], "from": m.get("from"), "subject": m.get("subject")} for m in res.sources],
        "sections": {k: [i.as_dict() for i in v] for k, v in res.sections.items()},
        "alerts": res.alerts,
        "cleanup": None if res.cleanup is None else {
            "archived": res.cleanup.archived, "scheduled_trash": res.cleanup.scheduled_trash,
            "trashed": res.cleanup.trashed, "kept": res.cleanup.kept, "errors": res.cleanup.errors},
    }
    if as_json:
        _print_json(payload)
    else:
        if res.status in ("dry_run", "built") or (dry_run and res.rendered):
            for title, text, rows in res.rendered:
                ch.console.rule(title)
                ch.console.print(text, markup=False)
                for row in rows:
                    ch.dim("   " + " · ".join(f"\\[{b['text']}]" for b in row))
        line = f"{res.status}: {res.item_count} item(s) from {len(res.sources)} email(s)" + (
            f" — {res.reason}" if res.reason else "")
        {"delivered": ch.success, "failed": ch.error}.get(res.status, ch.info)(line)
        if res.cleanup is not None:
            ch.info(f"Archived {len(res.cleanup.archived)}, kept {len(res.cleanup.kept)}"
                    + (f", Trash scheduled {len(res.cleanup.scheduled_trash)}" if res.cleanup.scheduled_trash else ""))
            for mid, why in list(res.cleanup.kept.items())[:5]:
                ch.dim(f"   kept {mid[:10]}: {why}")
        for a in res.alerts:
            ch.info(f"Deadline alert sent: {a}")
    if res.status == "failed":
        raise typer.Exit(1)


@brief_app.command("items")
def cmd_items(
    status: Annotated[str | None, typer.Option("--status", help="new | delivered | saved | dismissed")] = None,
    kind: Annotated[str | None, typer.Option("--kind", help="product | event | job | article")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 30,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Items the briefing has extracted, newest first."""
    from navig_email.store import get_store

    where, params = [], []
    if status:
        where.append("status = ?")
        params.append(status)
    if kind:
        where.append("kind = ?")
        params.append(kind)
    sql = "SELECT * FROM digest_items" + (" WHERE " + " AND ".join(where) if where else "")
    rows = [dict(r) for r in get_store()._read_all(sql + " ORDER BY first_seen DESC LIMIT ?", (*params, limit))]
    if as_json:
        _print_json({"items": [{**{k: r[k] for k in ("item_id", "kind", "title", "status", "score", "first_seen")},
                                "data": _json.loads(r["data_json"] or "{}")} for r in rows]})
        return
    if not rows:
        ch.info(f"No briefing items yet — `{CMD} brief run --space <space> --dry-run`")
        return
    t = Table(box=None, show_header=True, padding=(0, 2))
    for col, nw in (("Item", True), ("Kind", True), ("Status", True), ("Title", False)):
        t.add_column(col, no_wrap=nw)
    for r in rows:
        t.add_row(r["item_id"][:10], r["kind"], r["status"], r["title"][:80])
    ch.console.print(t)


def _set_status(item: str, status: str) -> None:
    from navig_email.store import get_store

    s = get_store()
    row = s._read_one("SELECT item_id FROM digest_items WHERE item_id LIKE ?", (item + "%",))
    if row is None:
        ch.error(f"no briefing item {item!r}")
        raise typer.Exit(1)
    s._write("UPDATE digest_items SET status = ? WHERE item_id = ?", (status, row["item_id"]))
    s.audit(f"brief.{status}", actor="user:cli", ref=row["item_id"])
    ch.success(f"{row['item_id'][:10]} → {status}")


@brief_app.command("save")
def cmd_save(item: Annotated[str, typer.Argument(help="Item id (prefix).")]) -> None:
    """Keep an item: its source email is never archived or trashed while it is saved."""
    _set_status(item, "saved")


@brief_app.command("dismiss")
def cmd_dismiss(item: Annotated[str, typer.Argument(help="Item id (prefix).")]) -> None:
    """Not interested: the item is not shown again."""
    _set_status(item, "dismissed")


@brief_app.command("extract")
def cmd_extract(
    message: Annotated[str, typer.Argument(help="A Gmail message id.")],
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """What the briefing extracts from one email (no model, nothing stored)."""
    from navig_email import account, messages
    from navig_email.brief import extract as X

    async def _go():
        c = await account.connector()
        raw = await c.get_message(message, format="full")
        return messages.shape(raw, body=c._extract_body(raw.get("payload") or {}))

    try:
        msg = account.run(_go())
    except account.NotConnected as exc:
        ch.error(str(exc))
        raise typer.Exit(1) from exc
    msg["body"] = X.undouble(msg.get("body") or "")
    src = X.source_of(msg, account.connected_email())
    items = []
    for _name, detect, parse in X.PARSERS:
        if detect(msg):
            items = parse(msg, src)
            if items:
                break
    if as_json:
        _print_json({"items": [i.as_dict() for i in items]})
        return
    if not items:
        ch.warning("No parser recognised this email (a configured local model would be tried in `run`).")
        return
    from navig_email.brief import render as R

    for it in items:
        ch.console.print(R.item_text(it), markup=False)


@brief_app.command("restore")
def cmd_restore(message: Annotated[str, typer.Argument(help="A Gmail message id the briefing archived or trashed.")]) -> None:
    """Bring a newsletter the briefing archived (or moved to Trash) back to the inbox."""
    from navig_email import account, cleanup

    async def _go():
        c = await account.connector()
        return await cleanup.restore(c, account=account.connected_email(), message_id=message)

    try:
        ok = account.run(_go())
    except account.NotConnected as exc:
        ch.error(str(exc))
        raise typer.Exit(1) from exc
    if not ok:
        ch.error(f"{message} was not archived or trashed by the briefing")
        raise typer.Exit(1)
    ch.success(f"{message} is back in the inbox.")


def _cron(method: str, path: str, *, action: str, json_body: dict | None = None) -> dict:
    """The local gateway's cron API through navig's public client; any failure exits non-zero."""
    try:
        from navig.gateway_client import gateway_request, unwrap_envelope
    except ImportError as exc:
        ch.error("scheduling needs navig (its cron service runs the briefing)")
        raise typer.Exit(1) from exc
    try:
        resp = gateway_request(method, path, json=json_body, timeout=10)
    except Exception as exc:  # noqa: BLE001 — not running / timed out: say so, exit 1
        ch.error(f"Cannot {action}: the navig gateway is not reachable ({type(exc).__name__})")
        ch.info("Start it with: navig gateway start")
        raise typer.Exit(1) from exc
    if resp.status_code >= 400:
        ch.error(f"Cannot {action}: gateway answered {resp.status_code}")
        raise typer.Exit(1)
    try:
        data = unwrap_envelope(resp.json())
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {"jobs": data}


@brief_app.command("schedule")
def cmd_schedule(
    space: Annotated[str, typer.Option("--space", help=_SPACE_HELP)],
    apply: Annotated[bool, typer.Option("--apply", help="Make the change (default: show the plan).")] = False,
) -> None:
    """Move the weekly template digest to the briefing: one job that decides itself when it is due."""
    from navig_email.brief import schedule as SCH

    paths = _paths(space)
    try:
        cfg = SCH.BriefConfig.from_config((paths.config.get("mailroom") or {}).get("brief"))
    except ValueError as exc:
        ch.error(str(exc))
        raise typer.Exit(2) from exc
    jobs = _cron("GET", "/cron/jobs", action="list jobs").get("jobs", [])
    old = [j for j in jobs if j.get("name") == OLD_JOB]
    new = [j for j in jobs if j.get("name") == NEW_JOB]
    command = f"navig email brief run --space {space} --scheduled"
    ch.info(f"Briefing: {cfg.frequency}, {['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][cfg.weekday]} "
            f"{cfg.at:%H:%M} {cfg.timezone}; ≥{cfg.min_items} items or postponed (≤{cfg.max_postpone_days} days)")
    plan = []
    if not new:
        plan.append(f"add job {NEW_JOB}: every 30 minutes → {command}")
    elif new[0].get("command") != command:
        plan.append(f"{NEW_JOB} exists with another command ({new[0].get('command')}) — left as is")
    for j in old:
        if j.get("enabled", True):
            plan.append(f"disable {OLD_JOB} ({j.get('id')}): it would send a second weekly digest")
    if not plan:
        ch.success("Already scheduled — nothing to change.")
        return
    for p in plan:
        ch.console.print(f"  • {p}")
    if not apply:
        ch.dim("Dry run. Re-run with --apply to make these changes (the old job is disabled, not removed).")
        return
    if not new:
        _cron("POST", "/cron/jobs", action="create job",
              json_body={"name": NEW_JOB, "schedule": "every 30 minutes", "command": command, "enabled": True})
    for j in old:
        if j.get("enabled", True):
            _cron("POST", f"/cron/jobs/{j['id']}/disable", action=f"disable job {j['id']}")
    ch.success(f"Scheduled. Undo: `navig cron enable <{OLD_JOB} id>` and `navig cron remove <{NEW_JOB} id>`.")
