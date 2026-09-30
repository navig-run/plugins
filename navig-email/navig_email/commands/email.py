"""navig email — the Gmail side of the mailroom.

House style: Rich tables plus a ``--json`` twin. This module is CLI only; every
decision lives in the sibling modules (rules, watch, stats, replied, followup), so
the verbs stay readable and the logic stays testable without a terminal.

Everything reads through navig's Gmail OAuth connector (``navig connector connect
gmail``). No model is consulted unless a command says ``--llm`` or ``--draft``, and
those print the provider and refuse a cloud one without ``--allow-cloud``.
"""

from __future__ import annotations

import json as _json
from datetime import date
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.table import Table

from navig_sdk import console as ch

from navig_sdk.host import command_name  # noqa: E402

# The command a user types here: `navig email` inside navig, `navig-email` on its own.
CMD = command_name("email")

email_app = typer.Typer(
    help="📧 Gmail mailroom: list/search/read, labels, rules → watch, stats, digests, "
         "spam-reply ledger. Local-first; models only on explicit opt-in.",
    no_args_is_help=True,
)
labels_app = typer.Typer(help="Gmail labels.", no_args_is_help=True)
rules_app = typer.Typer(help="Mailroom rules (mailroom/rules.yaml).", no_args_is_help=True)
edge_app = typer.Typer(
    help="The Cloudflare edge Worker on the domain's aliases (support@…): stats, events, send-as, deploy.",
    no_args_is_help=True,
)
email_app.add_typer(labels_app, name="labels")
email_app.add_typer(rules_app, name="rules")
email_app.add_typer(edge_app, name="edge")
imap_app = typer.Typer(help="Gmail with an app password (IMAP) — works without navig.", no_args_is_help=True)
email_app.add_typer(imap_app, name="imap")

_SPACE_HELP = "Space name or absolute path (holds mailroom/ and .navig/email/)."


@email_app.callback()
def _email_group(
    account: Annotated[
        str | None,
        typer.Option("--account", "-A", help="Which linked Gmail account (email). Default: the first one linked."),
    ] = None,
) -> None:
    """Gmail mailroom. `navig email --account other@gmail.com <command>` selects a linked account."""
    from navig_email import account as acct

    acct.use(account)


# ── helpers ───────────────────────────────────────────────────────────────


def _paths(space: str):
    from navig_email.space import paths_for

    try:
        return paths_for(space)
    except ValueError as exc:
        ch.error(str(exc))
        raise typer.Exit(2) from exc


def _connector():
    from navig_email import account

    try:
        return account.run(account.connector())
    except account.NotConnected as exc:
        ch.error(str(exc))
        raise typer.Exit(1) from exc


def _run(coro):
    from navig_email import account

    try:
        return account.run(coro)
    except account.NotConnected as exc:
        ch.error(str(exc))
        raise typer.Exit(1) from exc


def _rules(paths):
    from navig_email.rules_file import load_rules

    try:
        return load_rules(paths)
    except ValueError as exc:
        ch.error(str(exc))
        raise typer.Exit(2) from exc


def _print_json(payload: Any) -> None:
    ch.console.print_json(_json.dumps(payload, default=str))


def _folder_query(folder: str | None, query: str | None) -> str:
    parts = []
    if folder and folder != "all":
        parts.append(f"in:{folder}")
    if query:
        parts.append(query)
    return " ".join(parts) or "newer_than:7d"


def _messages_table(rows: list[dict[str, Any]], title: str) -> Table:
    from navig_email import messages as M

    t = Table(title=title)
    for col in ("Date", "De", "Objet", "Labels", "Id"):
        t.add_column(col, overflow="fold")
    for r in rows:
        labels = ",".join(x for x in r.get("labels", []) if x not in ("CATEGORY_PERSONAL",))
        t.add_row(r.get("date", "")[:16], (M.display_name(r.get("from", "")) or "?")[:32],
                  (r.get("subject") or "(sans objet)")[:70], labels[:40], r.get("id", ""))
    return t


async def _fetch_shaped(c, query: str, limit: int, *, spam: bool) -> list[dict[str, Any]]:
    from navig_email import messages as M

    entries = await c.iter_message_ids(query, limit=limit, include_spam_trash=spam)
    raw = await c.get_many([e["id"] for e in entries], format="metadata")
    return [M.shape(m) for m in raw]


def _ai_defaults(paths, model: str | None, allow_cloud: bool) -> tuple[str | None, bool]:
    """Fill --model / --allow-cloud from the space's ``mailroom.ai`` when not given.

    The pin matters: without an explicit model the mode router decides, and its fast-chat
    bypass routes `chat` to a cloud provider whenever such a key exists.
    """
    if paths is None:
        return model, allow_cloud
    return (model or paths.ai_model or None), (allow_cloud or paths.ai_allow_cloud)


def _telegram(text: str) -> bool:
    from navig.messaging.notify_operator import notify_operator

    return notify_operator(text)


# ── status / connect ──────────────────────────────────────────────────────


@email_app.command("status")
def cmd_status(
    space: Annotated[str | None, typer.Option("--space", help=_SPACE_HELP)] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Connected account, mailbox size, rules loaded, last watch."""
    from navig_email import account

    payload: dict[str, Any] = {"connected": account.is_connected(), "account": account.connected_email(),
                               "linked": account.linked_accounts()}
    if payload["connected"]:
        try:
            c = account.run(account.connector())
            payload["profile"] = account.run(account.profile(c))
        except Exception as exc:  # noqa: BLE001 — status must report, not crash
            payload["profile_error"] = str(exc)
    if space:
        from navig_email import state as S

        paths = _paths(space)
        st = S.load(paths)
        payload["space"] = str(paths.space_root)
        payload["rules"] = len(_rules(paths))
        payload["rules_file"] = str(paths.rules_yaml)
        payload["watch"] = {"seeded": st["seeded"], "history_id": st["history_id"],
                            "last_watch": st["last_watch"], "last_error": st["last_error"]}
    if as_json:
        _print_json(payload)
        return
    if not payload["connected"]:
        from navig_sdk.host import navig_available

        ch.error("Gmail: not connected. "
                 + ("Run `navig connector connect gmail` (OAuth), or " if navig_available() else "Run ")
                 + f"`{CMD} imap connect you@gmail.com` with a Gmail app password.")
        raise typer.Exit(1)
    ch.success(f"Gmail connected: {payload['account'] or '?'}"
               + (f" (linked: {', '.join(payload['linked'])})" if len(payload["linked"]) > 1 else ""))
    prof = payload.get("profile") or {}
    if prof:
        ch.kv("Messages", prof.get("messages_total"))
        ch.kv("Threads", prof.get("threads_total"))
    if space:
        ch.kv("Rules", f"{payload['rules']} ({payload['rules_file']})")
        w = payload["watch"]
        ch.kv("Watch", f"seeded={w['seeded']} last={w['last_watch'] or '—'}"
                       + (f" error={w['last_error']}" if w["last_error"] else ""))


@email_app.command("connect")
def cmd_connect() -> None:
    """Link a Gmail account: OAuth inside navig, a Gmail app password on its own."""
    from navig_sdk.host import navig_available

    if not navig_available():
        ch.info("On its own, navig-email signs in to Gmail with an app password:")
        ch.dim(f"  {CMD} imap connect you@gmail.com   (Google Account → Security → App passwords)")
        return
    from navig.commands.connector_cmd import connector_connect

    connector_connect("gmail")


# ── imap: Gmail with an app password (works with or without navig) ─────────


@imap_app.command("connect")
def imap_connect(
    user: Annotated[str, typer.Argument(help="Your Gmail address.")],
    password: Annotated[str | None, typer.Option(
        "--password", help="The app password (prompted, hidden, when omitted).")] = None,
) -> None:
    """Sign in to Gmail over IMAP with an app password, and keep it in the encrypted vault."""
    from navig_email import imap_account
    from navig_email.errors import ConnectorAPIError, ConnectorAuthError
    from navig_email.imap import GmailImap

    secret = password or typer.prompt("Gmail app password", hide_input=True)
    gm = GmailImap(user, secret.replace(" ", ""))
    try:
        prof = _run(gm.get_profile())  # prove it works BEFORE saving anything
    except (ConnectorAuthError, ConnectorAPIError, OSError) as exc:
        ch.error("Gmail refused the sign-in", str(exc))
        raise typer.Exit(1) from exc
    finally:
        gm.close()
    imap_account.save(user, secret)
    ch.success(f"Gmail connected over IMAP: {user}", f"{prof.get('messagesTotal', 0)} messages in All Mail")


@imap_app.command("status")
def imap_status(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Whether an app password is stored, and where it comes from (never shows it)."""
    from navig_email import imap_account

    creds = imap_account.load()
    payload = {"configured": bool(creds), "user": creds[0] if creds else None, "source": imap_account.source()}
    if as_json:
        _print_json(payload)
        return
    if not creds:
        ch.warning("No Gmail app password stored", f"Run: {CMD} imap connect you@gmail.com")
        raise typer.Exit(1)
    ch.success(f"Gmail over IMAP: {creds[0]}", f"from the {payload['source']}")


@imap_app.command("disconnect")
def imap_disconnect() -> None:
    """Forget the stored app password."""
    from navig_email import imap_account

    if imap_account.remove():
        ch.success("Removed the stored Gmail app password.")
    else:
        ch.info("No Gmail app password was stored.")


# ── read ──────────────────────────────────────────────────────────────────


@email_app.command("list")
def cmd_list(
    folder: Annotated[str, typer.Option("--folder", "-f", help="inbox | spam | sent | trash | all")] = "inbox",
    query: Annotated[str | None, typer.Option("--query", "-q", help="Extra Gmail search terms.")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List messages of a folder (newest first)."""
    c = _connector()
    q = _folder_query(folder, query)
    rows = _run(_fetch_shaped(c, q, limit, spam=folder in ("spam", "trash", "all")))
    if as_json:
        _print_json(rows)
        return
    if not rows:
        ch.info(f"No messages for `{q}`.")
        return
    ch.console.print(_messages_table(rows, f"{len(rows)} message(s) — {q}"))


@email_app.command("search")
def cmd_search(
    query: Annotated[str, typer.Argument(help="Gmail search syntax, e.g. 'from:caf.fr newer_than:30d'.")],
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Search with Gmail's own syntax (spam and trash included)."""
    c = _connector()
    rows = _run(_fetch_shaped(c, query, limit, spam=True))
    if as_json:
        _print_json(rows)
        return
    if not rows:
        ch.info(f"No messages for `{query}`.")
        return
    ch.console.print(_messages_table(rows, f"{len(rows)} message(s) — {query}"))


@email_app.command("read")
def cmd_read(
    message_id: Annotated[str, typer.Argument(help="Message id (from list/search).")],
    thread: Annotated[bool, typer.Option("--thread", help="Whole thread.")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Read one message (or its whole thread), body included."""
    from navig_email import messages as M

    c = _connector()

    async def _go():
        if thread:
            first = await c.get_message(message_id, format="minimal")
            t = await c.get_thread(first.get("threadId", ""), format="full")
            return [M.shape(m, body=c._extract_body(m.get("payload") or {})) for m in t.get("messages") or []]
        m = await c.get_message(message_id, format="full")
        return [M.shape(m, body=c._extract_body(m.get("payload") or {}))]

    rows = _run(_go())
    if as_json:
        _print_json(rows)
        return
    for r in rows:
        ch.console.rule(f"{r['date'][:16]} · {r['from']}")
        ch.kv("Objet", r["subject"])
        ch.kv("À", r["to"])
        ch.kv("Labels", ", ".join(r["labels"]))
        ch.console.print()
        ch.console.print((r["body"] or r["snippet"] or "").strip()[:6000])
        ch.console.print()


# ── labels / tag ──────────────────────────────────────────────────────────


@labels_app.command("list")
def labels_list(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Every label with its counts."""
    c = _connector()
    labels = _run(c.list_labels())
    rows = sorted(labels, key=lambda x: (x.get("type", ""), str(x.get("name", "")).lower()))
    if as_json:
        _print_json(rows)
        return
    t = Table(title=f"{len(rows)} label(s)")
    for col in ("Name", "Id", "Type"):
        t.add_column(col)
    for lbl in rows:
        t.add_row(str(lbl.get("name", "")), str(lbl.get("id", "")), str(lbl.get("type", "")))
    ch.console.print(t)


@labels_app.command("ensure")
def labels_ensure(name: Annotated[str, typer.Argument(help="Label name, nested with '/'.")]) -> None:
    """Create a label (and its parents) if missing; print its id."""
    c = _connector()
    lid = _run(c.ensure_label(name))
    ch.success(f"{name} → {lid}")


@email_app.command("tag")
def cmd_tag(
    message_id: Annotated[str | None, typer.Argument(help="Message id, or use --query.")] = None,
    query: Annotated[str | None, typer.Option("--query", "-q", help="Tag every message matching this search.")] = None,
    add: Annotated[list[str] | None, typer.Option("--add", "-a", help="Label to add (repeatable).")] = None,
    remove: Annotated[list[str] | None, typer.Option("--remove", "-r", help="Label to remove (repeatable).")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 100,
    yes: Annotated[bool, typer.Option("--yes", help="Required with --query.")] = False,
) -> None:
    """Add/remove labels on one message or on a search result."""
    if not (message_id or query) or (message_id and query):
        ch.error("give a message id OR --query")
        raise typer.Exit(2)
    if not (add or remove):
        ch.error("nothing to do: --add or --remove")
        raise typer.Exit(2)
    if query and not yes:
        ch.error("--query changes many messages: add --yes")
        raise typer.Exit(2)
    c = _connector()

    async def _go():
        add_ids = [await c.ensure_label(n) for n in (add or [])]
        rem_ids = [await c.ensure_label(n) for n in (remove or [])]
        ids = [message_id] if message_id else [e["id"] for e in await c.iter_message_ids(query, limit=limit, include_spam_trash=True)]
        ok = 0
        for mid in ids:
            res = await c.modify_message(mid, add=add_ids or None, remove=rem_ids or None)
            ok += 1 if res.success else 0
        return ok, len(ids)

    ok, total = _run(_go())
    if ok != total:
        ch.error(f"tagged {ok}/{total}")
        raise typer.Exit(1)
    ch.success(f"tagged {ok} message(s)")


# ── rules ─────────────────────────────────────────────────────────────────


@rules_app.command("list")
def rules_list(
    space: Annotated[str, typer.Option("--space", help=_SPACE_HELP)],
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """The rules the watch applies (space file + enabled daemon rules)."""
    from navig_email import rules as R

    paths = _paths(space)
    rules = _rules(paths)
    if as_json:
        _print_json(rules)
        return
    t = Table(title=f"{len(rules)} rule(s) — {paths.rules_yaml}")
    for col in ("Id", "Conditions", "Actions", "Source"):
        t.add_column(col, overflow="fold")
    for r in rules:
        n = R.normalize(r)
        conds = "; ".join(f"{k}={n[k]}" for k in R.CONDITION_KEYS if n.get(k) not in (None, "", []))
        t.add_row(str(n.get("id")), conds, ", ".join(str(a) for a in R.actions_for(n)) or "—", n.get("source", "space"))
    ch.console.print(t)


@rules_app.command("check")
def rules_check(
    space: Annotated[str, typer.Option("--space", help=_SPACE_HELP)],
    message_id: Annotated[str | None, typer.Argument(help="Optional: which rules match this message.")] = None,
) -> None:
    """Validate the rules file; with a message id, show which rules would fire."""
    from navig_email import messages as M, rules as R
    from navig_email.rules_file import check_rules

    paths = _paths(space)
    problems = check_rules(paths)
    for p in problems:
        ch.warning(p)
    if not message_id:
        if problems:
            ch.error(f"{len(problems)} problem(s) in {paths.rules_yaml}")
            raise typer.Exit(1)
        ch.success(f"{paths.rules_yaml}: OK")
        return
    c = _connector()
    m = _run(c.get_message(message_id, format="full"))
    msg = M.shape(m, body=c._extract_body(m.get("payload") or {}))
    hits = R.all_matches(msg, _rules(paths))
    if not hits:
        ch.info("no rule matches this message")
        return
    for r in hits:
        ch.success(f"{r.get('id')} → {', '.join(str(a) for a in R.actions_for(r))}")


@rules_app.command("apply")
def rules_apply(
    space: Annotated[str, typer.Option("--space", help=_SPACE_HELP)],
    query: Annotated[str, typer.Option("--query", "-q", help="Messages to run the rules over.")] = "newer_than:7d",
    limit: Annotated[int, typer.Option("--limit", "-n")] = 200,
    yes: Annotated[bool, typer.Option("--yes")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run the rules over existing mail (backfill labels). Notifications are NOT sent here."""
    from navig_email import actions as A, messages as M, rules as R

    paths = _paths(space)
    rules = _rules(paths)
    if not (yes or dry_run):
        ch.error("this changes labels: add --yes (or --dry-run)")
        raise typer.Exit(2)
    c = _connector()

    async def _go():
        fmt = "full" if R.needs_body(rules) else "metadata"
        entries = await c.iter_message_ids(query, limit=limit, include_spam_trash=True)
        raw = await c.get_many([e["id"] for e in entries], format=fmt)
        out = A.Outcome()
        labels = A.LabelCache(c)
        matched = 0
        for m in raw:
            msg = M.shape(m, body=c._extract_body(m.get("payload") or {}) if fmt == "full" else "")
            hits = R.all_matches(msg, rules)
            matched += bool(hits)
            for rule in hits:
                acts = [a for a in R.actions_for(rule) if a.kind != "notify"]
                await A.apply(c, msg, rule, acts, labels=labels, dry_run=dry_run, out=out)
        return len(raw), matched, out

    scanned, matched, out = _run(_go())
    payload = {"dry_run": dry_run, "scanned": scanned, "matched": matched, "labelled": out.labelled,
               "starred": out.starred, "archived": out.archived, "marked_read": out.marked_read,
               "ran": out.ran, "errors": out.errors, "planned": out.planned if dry_run else []}
    if as_json:
        _print_json(payload)
    else:
        verb = "Would change" if dry_run else "Changed"
        ch.success(f"{scanned} scanned · {matched} matched · {verb} {out.changes}")
        for p in (out.planned if dry_run else [])[:50]:
            ch.dim(p)
        for e in out.errors[:20]:
            ch.warning(e)
    if out.errors:
        raise typer.Exit(1)


# ── watch ─────────────────────────────────────────────────────────────────


@email_app.command("watch")
def cmd_watch(
    space: Annotated[str, typer.Option("--space", help=_SPACE_HELP)],
    yes: Annotated[bool, typer.Option("--yes", help="Apply actions and send notifications.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Report what would happen; touch nothing.")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """One incremental pass over new mail: rules → labels / Telegram / run. For cron (every 5 min)."""
    from navig_email import watch as W

    paths = _paths(space)
    rules = _rules(paths)
    if not (yes or dry_run):
        ch.error("watch changes labels and sends notifications: add --yes (or --dry-run)")
        raise typer.Exit(2)
    res = _run(W.run(paths, rules, dry_run=dry_run))
    o = res.outcome
    payload = {"seeded": res.seeded, "source": res.source, "candidates": res.candidates, "new": res.new,
               "matched": res.matched, "labelled": o.labelled, "starred": o.starred, "archived": o.archived,
               "marked_read": o.marked_read, "ran": o.ran, "notifications": len(o.notifications),
               "notified": res.notified, "cursor_advanced": res.cursor_advanced, "dry_run": dry_run,
               "errors": o.errors, "planned": o.planned if dry_run else []}
    if as_json:
        _print_json(payload)
    elif res.seeded:
        ch.success(f"Seeded: {res.candidates} recent message(s) marked as seen, cursor set. "
                   "Next pass notifies on new mail only.")
    else:
        ch.success(f"{res.source}: {res.candidates} candidate(s), {res.new} new, {res.matched} matched · "
                   f"labels {o.labelled} · notifications {len(o.notifications)}"
                   + (f" ({'sent' if res.notified else 'FAILED'})" if res.notified is not None else ""))
        for p in (o.planned if dry_run else [])[:50]:
            ch.dim(p)
        for e in o.errors[:20]:
            ch.warning(e)
    if o.errors or res.notified is False:
        raise typer.Exit(1)


@email_app.command("sync", hidden=True)
def cmd_sync(
    space: Annotated[str, typer.Option("--space", help=_SPACE_HELP)],
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Alias of `watch`."""
    cmd_watch(space=space, yes=yes, dry_run=not yes, as_json=False)


# ── stats / digest ────────────────────────────────────────────────────────


def _period(period: str, since: str | None, until: str | None):
    from navig_email import stats as ST

    try:
        s = date.fromisoformat(since) if since else None
        u = date.fromisoformat(until) if until else None
        return ST.period_bounds(period, since=s, until=u)
    except ValueError as exc:
        ch.error(str(exc))
        raise typer.Exit(2) from exc


@email_app.command("stats")
def cmd_stats(
    space: Annotated[str, typer.Option("--space", help=_SPACE_HELP)],
    period: Annotated[str, typer.Option("--period", "-p", help="day | week | month")] = "week",
    since: Annotated[str | None, typer.Option("--since", help="YYYY-MM-DD (overrides --period)")] = None,
    until: Annotated[str | None, typer.Option("--until", help="YYYY-MM-DD")] = None,
    send: Annotated[bool, typer.Option("--send", help="Telegram summary.")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Counts for the period (received/sent/spam/unread, per label & rule, top senders, per day)."""
    from navig_email import account, stats as ST

    paths = _paths(space)
    per = _period(period, since, until)
    c = _connector()
    st = _run(ST.compute(c, per, rule_list=_rules(paths)))
    paths.reports_dir.mkdir(parents=True, exist_ok=True)
    report = paths.reports_dir / f"stats-{per.name}-{per.since.isoformat()}.md"
    md = ST.render_markdown(st, account=account.connected_email())
    tg = ST.telegram_text(st)
    # The edge Worker's own counters (the aliases routed through it), when configured.
    try:
        from navig_email import edge as E

        edge_st = E.stats(paths, days=max(1, (per.until - per.since).days))
        st["edge"] = edge_st
        md += "\n" + E.stats_markdown(edge_st, url=E.edge_url(paths))
        tg += "\n" + E.stats_telegram(edge_st)
    except Exception as exc:  # noqa: BLE001 — the edge is optional; the report must still land
        st["edge_error"] = str(exc)[:200]
    report.write_text(md, encoding="utf-8")
    st["report"] = str(report)
    sent = _telegram(tg) if send else None
    st["telegram"] = sent
    if as_json:
        _print_json(st)
    else:
        cnt = st["counts"]
        ch.success(f"{per.label}: reçus {cnt['received']} · réception {cnt['inbox']} (non lus {cnt['unread']}) · "
                   f"envoyés {cnt['sent']} · spam {cnt['spam']}")
        for k, v in {**st["rules"], **st["labels"]}.items():
            ch.kv(k, v)
        for addr, n in st["top_senders"][:5]:
            ch.dim(f"  {addr}: {n}")
        ch.dim(f"Rapport → {report}")
        if sent is not None:
            ch.dim("Telegram: sent." if sent else "Telegram: FAILED")
    if sent is False:
        raise typer.Exit(1)


@email_app.command("digest")
def cmd_digest(
    space: Annotated[str, typer.Option("--space", help=_SPACE_HELP)],
    period: Annotated[str, typer.Option("--period", "-p", help="day | week | month")] = "week",
    llm: Annotated[bool, typer.Option("--llm", help="Add a prose brief (opt-in model call).")] = False,
    model: Annotated[str | None, typer.Option("--model", help="provider:model, e.g. ollama:llama3")] = None,
    allow_cloud: Annotated[bool, typer.Option("--allow-cloud", help="Permit a non-local provider.")] = False,
    focus: Annotated[str, typer.Option("--focus", help="What the prose should prioritise.")] = "",
    send: Annotated[bool, typer.Option("--send")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Template digest for the period; `--llm` adds prose through the guarded model door."""
    from navig_email import account, digest as D, llm_guard, stats as ST

    paths = _paths(space)
    per = _period(period, None, None)
    c = _connector()
    st = _run(ST.compute(c, per, rule_list=_rules(paths)))
    prose, provider = "", ""
    if llm:
        model, allow_cloud = _ai_defaults(paths, model, allow_cloud)
        try:
            prose, provider = _run(D.prose_summary(c, per, model=model, allow_cloud=allow_cloud, focus=focus))
        except llm_guard.CloudRefused as exc:
            ch.error(str(exc))
            raise typer.Exit(1) from exc
        ch.info(f"Modèle : {provider or '—'}")
    paths.reports_dir.mkdir(parents=True, exist_ok=True)
    report = paths.reports_dir / f"digest-{per.name}-{per.since.isoformat()}.md"
    report.write_text(D.render_markdown(st, account=account.connected_email(), prose=prose, provider=provider),
                      encoding="utf-8")
    sent = _telegram(D.telegram_text(st, prose=prose)) if send else None
    if as_json:
        _print_json({"period": st["period"], "counts": st["counts"], "rules": st["rules"], "labels": st["labels"],
                     "prose": prose, "provider": provider, "report": str(report), "telegram": sent})
    else:
        ch.console.print(report.read_text(encoding="utf-8"))
        ch.dim(f"Rapport → {report}")
        if sent is not None:
            ch.dim("Telegram: sent." if sent else "Telegram: FAILED")
    if sent is False:
        raise typer.Exit(1)


# ── send ──────────────────────────────────────────────────────────────────


@email_app.command("send")
def cmd_send(
    to: Annotated[str, typer.Option("--to", "-t", help="Recipient(s), comma-separated.")],
    subject: Annotated[str, typer.Option("--subject", "-s")],
    body: Annotated[str | None, typer.Option("--body", "-b", help="Body (else stdin).")] = None,
    reply_to: Annotated[str | None, typer.Option("--reply-to", help="Message id to reply to (threads it).")] = None,
    draft: Annotated[bool, typer.Option("--draft", help="Save as a Gmail draft instead of sending.")] = False,
    via: Annotated[str, typer.Option("--via", help="gmail (connector) | cloudflare (edge Worker, send AS support@<domain>)")] = "gmail",
    from_addr: Annotated[str | None, typer.Option("--from", help="cloudflare only: the @domain sender (default support@).")] = None,
    in_reply_to: Annotated[str | None, typer.Option("--in-reply-to", help="cloudflare only: Message-ID to thread on.")] = None,
    space: Annotated[str | None, typer.Option("--space", help=_SPACE_HELP)] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Required to actually send.")] = False,
) -> None:
    """Send (or draft) a message — through Gmail, or AS the domain via the Cloudflare edge."""
    from navig_email import account
    from navig_email.errors import Action, ActionType

    text = body
    if text is None:
        import sys

        text = sys.stdin.read()
    if not text.strip():
        ch.error("empty body")
        raise typer.Exit(2)
    if via == "cloudflare":
        from navig_email import edge as E

        if draft:
            ch.error("--draft is a Gmail feature; the edge sends immediately")
            raise typer.Exit(2)
        if not yes:
            ch.error("this sends mail AS the domain: add --yes")
            raise typer.Exit(2)
        paths = _paths(space) if space else None
        try:
            res = E.send(paths, to=to, subject=subject, text=text, from_addr=from_addr, in_reply_to=in_reply_to)
        except (E.EdgeNotConfigured, RuntimeError, Exception) as exc:  # noqa: BLE001 — one line, exit 1
            ch.error(str(exc))
            raise typer.Exit(1) from exc
        ch.success(f"Sent to {to} as {from_addr or 'support@' + str(E.edge_config(paths).get('domain') or 'domain')}")
        ch.dim(_json.dumps(res.get("result", {}), default=str)[:300])
        return
    if via != "gmail":
        ch.error("--via must be gmail or cloudflare")
        raise typer.Exit(2)
    if not account.is_connected():
        # Legacy path: app-password SMTP configured with `navig email setup`.
        return _legacy_send(to, subject, text)
    if not (draft or yes):
        ch.error("this sends mail: add --yes (or --draft to save a draft)")
        raise typer.Exit(2)
    c = _connector()

    async def _go():
        if draft:
            in_reply_to = None
            thread_id = None
            if reply_to:
                orig = await c.get_message(reply_to, format="metadata", headers=["Message-ID"])
                thread_id = orig.get("threadId")
                for h in (orig.get("payload") or {}).get("headers") or []:
                    if h.get("name", "").lower() == "message-id":
                        in_reply_to = h.get("value")
            return await c.create_draft(to=to, subject=subject, body=text, thread_id=thread_id, in_reply_to=in_reply_to)
        if reply_to:
            return await c.act(Action(action_type=ActionType.REPLY, resource_id=reply_to, params={"body": text}))
        return await c.act(Action(action_type=ActionType.SEND, params={"to": to, "subject": subject, "body": text}))

    res = _run(_go())
    if draft:
        ch.success(f"Draft saved: {res.get('id', '?')}")
        return
    if not getattr(res, "success", False):
        ch.error(f"send failed: {getattr(res, 'error', '?')}")
        raise typer.Exit(1)
    ch.success(f"Sent to {to}")


def _legacy_send(to: str, subject: str, body: str) -> None:
    """App-password SMTP send (pre-OAuth path), kept for installs without the connector."""
    import os

    from navig.agent.proactive import GmailProvider, IMAPEmailProvider
    from navig.config import get_config_manager
    from navig.core.coerce import coerce_bool
    from navig_email import account

    cfg = (get_config_manager().get_global_config() or {}).get("proactive", {}).get("email", {})
    if not coerce_bool(cfg.get("enabled"), default=False):
        ch.error("Gmail is not connected and no SMTP account is configured.")
        ch.info(f"Connect with `navig connector connect gmail` (recommended) or `{CMD} setup`.")
        raise typer.Exit(1)
    provider_type = cfg.get("provider", "mock")
    email_addr = cfg.get("address")
    password = cfg.get("password") or os.environ.get("EMAIL_PASSWORD")
    if not email_addr or not password:
        ch.error("No email address / app-password available for sending.")
        raise typer.Exit(1)
    if provider_type == "gmail":
        provider = GmailProvider(email_address=email_addr, app_password=password)
    elif provider_type == "imap":
        provider = IMAPEmailProvider(
            email_address=email_addr, password=password, imap_host=cfg.get("imap_host"),
            smtp_host=cfg.get("smtp_host"), imap_port=int(cfg.get("imap_port", 993)),
            smtp_port=int(cfg.get("smtp_port", 465)),
        )
    else:
        ch.error(f"Sending not supported for provider: {provider_type}")
        raise typer.Exit(1)
    recipients = [x.strip() for x in to.split(",") if x.strip()]
    try:
        sent = account.run(provider.send_email(recipients, subject, body))
    except Exception as exc:  # noqa: BLE001
        ch.error(f"Send failed: {exc}")
        raise typer.Exit(1) from None
    if not sent:
        ch.error("Send did not complete")
        raise typer.Exit(1)
    ch.success(f"Email sent to {', '.join(recipients)}")


@email_app.command("setup", hidden=True)
def setup_email(
    provider: Annotated[str, typer.Argument(help="Provider: gmail, outlook, imap")] = "gmail",
) -> None:
    """Legacy: configure an app-password SMTP account. Prefer `navig email connect`."""
    from navig.config import get_config_manager
    from navig.core.yaml_io import atomic_write_yaml, load_yaml_for_update

    cm = get_config_manager()
    global_config_file = cm.global_config_dir / "config.yaml"
    config = load_yaml_for_update(global_config_file)
    config.setdefault("proactive", {}).setdefault("email", {})
    email_cfg = config["proactive"]["email"]
    ch.info(f"{provider.title()} Email Setup")
    email_cfg["address"] = typer.prompt("Email address")
    email_cfg["enabled"] = True
    email_cfg["provider"] = provider
    email_cfg["password"] = "${EMAIL_PASSWORD}"
    if provider == "imap":
        email_cfg["imap_host"] = typer.prompt("IMAP host (e.g., imap.gmail.com)")
        email_cfg["smtp_host"] = typer.prompt("SMTP host (e.g., smtp.gmail.com)")
    atomic_write_yaml(config, global_config_file)
    ch.success("✓ Email configured!")
    ch.warning("Set your password: export EMAIL_PASSWORD='your-password'")


# ── edge (Cloudflare Worker on the domain aliases) ────────────────────────


def _edge_paths(space: str | None):
    return _paths(space) if space else None


def _edge_fail(exc: Exception) -> None:
    ch.error(str(exc))
    raise typer.Exit(1) from exc


@edge_app.command("status")
def edge_status(
    space: Annotated[str | None, typer.Option("--space", help=_SPACE_HELP)] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Is the Worker up, and does it have Email Sending?"""
    from navig_email import edge as E

    paths = _edge_paths(space)
    try:
        url = E.edge_url(paths)
        h = E.health(paths)
    except Exception as exc:  # noqa: BLE001
        _edge_fail(exc)
        return
    payload = {"url": url, **h}
    if paths is not None:
        from navig_email import edge as EE

        payload["accounts_yaml_notify"] = EE.notify_aliases(paths)
    if as_json:
        _print_json(payload)
        return
    ch.success(f"{h.get('worker', 'edge')} up at {url} · sending={'yes' if h.get('sending') else 'no'}")
    if h.get("notify_aliases"):
        ch.kv("notify (live)", ", ".join(h["notify_aliases"]))
    wanted = payload.get("accounts_yaml_notify") or []
    if wanted and sorted(wanted) != sorted(h.get("notify_aliases") or []):
        ch.warning(f"accounts.yaml disagrees with the deployed list — run `{CMD} edge deploy --space …`")
    ch.kv("read-only token", "yes" if h.get("read_token") else "no (reads use the full token)")


@edge_app.command("stats")
def edge_stats(
    space: Annotated[str | None, typer.Option("--space", help=_SPACE_HELP)] = None,
    days: Annotated[int, typer.Option("--days", "-d")] = 7,
    send: Annotated[bool, typer.Option("--send", help="Telegram summary.")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Counts by alias / tag / day / sender domain for mail that entered at the edge."""
    from navig_email import edge as E

    paths = _edge_paths(space)
    try:
        st = E.stats(paths, days=days)
    except Exception as exc:  # noqa: BLE001
        _edge_fail(exc)
        return
    sent = _telegram(E.stats_telegram(st)) if send else None
    if as_json:
        _print_json({**st, "telegram": sent})
    else:
        ch.success(f"{st.get('total', 0)} message(s) in {days} day(s) · forwarded {st.get('forwarded', 0)} · notified {st.get('notified', 0)}")
        for a, n in sorted((st.get("by_alias") or {}).items(), key=lambda kv: -kv[1]):
            ch.kv(f"{a}@", n)
        for t, n in sorted((st.get("by_tag") or {}).items(), key=lambda kv: -kv[1]):
            ch.dim(f"  #{t}: {n}")
        if sent is not None:
            ch.dim("Telegram: sent." if sent else "Telegram: FAILED")
    if sent is False:
        raise typer.Exit(1)


@edge_app.command("events")
def edge_events(
    space: Annotated[str | None, typer.Option("--space", help=_SPACE_HELP)] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Recent messages the edge handled (metadata only — never bodies)."""
    from navig_email import edge as E

    paths = _edge_paths(space)
    try:
        rows = E.events(paths, limit=limit)
    except Exception as exc:  # noqa: BLE001
        _edge_fail(exc)
        return
    if as_json:
        _print_json(rows)
        return
    if not rows:
        ch.info("no events yet")
        return
    t = Table(title=f"{len(rows)} edge event(s)")
    for col in ("When", "Alias", "From", "Subject", "Tags", "Fwd", "TG"):
        t.add_column(col, overflow="fold")
    for r in rows:
        t.add_row(str(r.get("ts", ""))[:16], f"{r.get('alias')}@", (r.get("from_name") or r.get("from_addr") or "?")[:28],
                  (r.get("subject") or "")[:50], r.get("tags") or "", "✓" if r.get("forwarded") else "✗",
                  "✓" if r.get("notified") else "—")
    ch.console.print(t)


@edge_app.command("send")
def edge_send(
    to: Annotated[str, typer.Option("--to", "-t")],
    subject: Annotated[str, typer.Option("--subject", "-s")],
    body: Annotated[str | None, typer.Option("--body", "-b", help="Body (else stdin).")] = None,
    from_addr: Annotated[str | None, typer.Option("--from", help="@domain sender (default support@).")] = None,
    from_name: Annotated[str | None, typer.Option("--from-name")] = None,
    reply_to: Annotated[str | None, typer.Option("--reply-to")] = None,
    in_reply_to: Annotated[str | None, typer.Option("--in-reply-to", help="Message-ID to thread on.")] = None,
    space: Annotated[str | None, typer.Option("--space", help=_SPACE_HELP)] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Required to actually send.")] = False,
) -> None:
    """Send AS support@<domain> (or another @domain address) through Cloudflare Email Sending."""
    from navig_email import edge as E

    text = body
    if text is None:
        import sys

        text = sys.stdin.read()
    if not text.strip():
        ch.error("empty body")
        raise typer.Exit(2)
    if not yes:
        ch.error("this sends mail AS the domain: add --yes")
        raise typer.Exit(2)
    paths = _edge_paths(space)
    try:
        res = E.send(paths, to=to, subject=subject, text=text, from_addr=from_addr, from_name=from_name,
                     reply_to=reply_to, in_reply_to=in_reply_to)
    except Exception as exc:  # noqa: BLE001
        _edge_fail(exc)
        return
    ch.success(f"Sent to {to}")
    ch.dim(_json.dumps(res.get("result", {}), default=str)[:300])


@edge_app.command("deploy")
def edge_deploy(
    space: Annotated[str | None, typer.Option("--space", help=_SPACE_HELP)] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Build only, upload nothing.")] = False,
) -> None:
    """Deploy the Worker from the plugin's edge/ folder (uses wrangler's own login).

    With --space, the notify list and the forward address come from that space's
    mailroom/accounts.yaml, so `aliases[].notify` is the single source of truth.
    """
    from navig_email import edge as E

    paths = _edge_paths(space)
    try:
        code, out, overrides = E.deploy(dry_run=dry_run, paths=paths)
    except Exception as exc:  # noqa: BLE001
        _edge_fail(exc)
        return
    for key, value in overrides.items():
        ch.kv(key, value)
    if not overrides and space:
        ch.warning("no aliases with `notify: true` in accounts.yaml — the Worker keeps its baked-in list")
    for line in out.strip().splitlines()[-12:]:
        ch.dim(line)
    if code != 0:
        ch.error(f"wrangler deploy exited {code}")
        raise typer.Exit(1)
    ch.success("edge deployed" + (" (dry run)" if dry_run else ""))


@edge_app.command("secrets")
def edge_secrets(
    space: Annotated[str | None, typer.Option("--space", help=_SPACE_HELP)] = None,
    rotate_read: Annotated[bool, typer.Option("--rotate-read", help="Mint a NEW read-only token.")] = False,
    telegram: Annotated[bool, typer.Option("--telegram/--no-telegram", help="Also push the bot token + chat id.")] = True,
) -> None:
    """Push the Worker's secrets from navig's own vault/config. Nothing is ever printed.

    EDGE_TOKEN (full: reads + send-as) and EDGE_READ_TOKEN (reads only) live in the vault as
    `mailroom/edge_token` and `mailroom/edge_read_token`; missing ones are minted here.
    """
    import secrets as _secrets

    from navig_email import edge as E

    pushed: list[str] = []
    try:
        full = E._vault_secret(E.TOKEN_LABEL)
        if not full:
            full = _secrets.token_hex(32)
            E.put_secret(E.TOKEN_LABEL, full)
            ch.dim(f"minted {E.TOKEN_LABEL}")
        read = "" if rotate_read else E._vault_secret(E.READ_TOKEN_LABEL)
        if not read:
            read = _secrets.token_hex(32)
            E.put_secret(E.READ_TOKEN_LABEL, read)
            ch.dim(f"{'rotated' if rotate_read else 'minted'} {E.READ_TOKEN_LABEL}")
        wanted: list[tuple[str, str]] = [("EDGE_TOKEN", full), ("EDGE_READ_TOKEN", read)]
        if telegram:
            from navig.commands.telegram import _load_telegram_token
            from navig.messaging.notify_operator import resolve_operator_chat_id

            bot = (_load_telegram_token() or "").strip()
            chat = (resolve_operator_chat_id() or "").strip()
            if not bot or not chat:
                ch.warning("Telegram bot token / chat id not resolvable — skipping those two")
            else:
                wanted += [("TELEGRAM_BOT_TOKEN", bot), ("TELEGRAM_CHAT_ID", chat)]
        code, out = E.put_worker_secrets(dict(wanted), paths=_edge_paths(space))
        if code != 0:
            tail = out.strip().splitlines()[-1] if out.strip() else ""
            ch.error(f"wrangler secret bulk exited {code}: {tail}")
            raise typer.Exit(1)
        pushed = [name for name, _ in wanted]
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001
        _edge_fail(exc)
        return
    ch.success(f"pushed {len(pushed)} secret(s): {', '.join(pushed)}")


# ── replied / followup (the spam corpus) ──────────────────────────────────


@email_app.command("replied")
def cmd_replied(
    space: Annotated[str, typer.Option("--space", help=_SPACE_HELP)],
    folder: Annotated[str, typer.Option("--in", help="Folder the originals were in.")] = "spam",
    since: Annotated[str, typer.Option("--since", help="YYYY-MM-DD — how far back to read Sent.")] = "2024-01-01",
    ledger_name: Annotated[str, typer.Option("--ledger", help="Ledger / corpus name.")] = "piratebay",
    label: Annotated[str, typer.Option("--label", help="Gmail label for these threads.")] = "Cybesis/PirateBay",
    apply_labels: Annotated[bool, typer.Option("--apply-labels", help="Apply the label in Gmail.")] = False,
    include_likely: Annotated[bool, typer.Option("--include-likely", help="Also label the 'likely' tier.")] = False,
    send: Annotated[bool, typer.Option("--send", help="Telegram stats digest.")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Find every Sent reply to mail that sat in a folder (spam): ledger, corpus, label, stats."""
    from navig_email import account, ledger as L, replied as RP

    paths = _paths(space)
    try:
        since_d = date.fromisoformat(since)
    except ValueError as exc:
        ch.error(f"--since: {exc}")
        raise typer.Exit(2) from exc
    c = _connector()
    my_email = account.connected_email() or _run(account.profile(c))["email"]

    async def _go():
        found = await RP.find(c, since=since_d, folder=folder, my_email=my_email)
        labelled = 0
        if apply_labels and found:
            lid = await c.ensure_label(label)
            for rt in found:
                if rt.tier == RP.TIER_CONFIRMED or include_likely:
                    res = await c.modify_thread(rt.thread_id, add=[lid])
                    if res.success:
                        rt.labels_applied = sorted(set(rt.labels_applied) | {label})
                        labelled += 1
        return found, labelled

    found, labelled = _run(_go())
    rows = [rt.to_row() for rt in found]
    ledger_path = paths.ledger(ledger_name)
    added, updated = L.upsert(ledger_path, rows, key="thread_id") if rows else (0, 0)
    all_rows = L.read_jsonl(ledger_path)
    corpus = RP.write_corpus(all_rows, paths.corpus_dir(ledger_name) / "corpus.md",
                             title=f"{ledger_name} — corpus")
    st = RP.compute_stats(all_rows)
    paths.reports_dir.mkdir(parents=True, exist_ok=True)
    report = paths.reports_dir / f"{ledger_name}-stats.md"
    report.write_text(RP.stats_markdown(st, ledger_name=ledger_name), encoding="utf-8")
    sent = _telegram(RP.telegram_text(st, ledger_name=ledger_name)) if send else None

    payload = {"found": len(found), "confirmed": sum(1 for r in found if r.tier == RP.TIER_CONFIRMED),
               "likely": sum(1 for r in found if r.tier == RP.TIER_LIKELY),
               "answered_back": sum(1 for r in found if r.answered_back), "labelled": labelled,
               "ledger": str(ledger_path), "ledger_added": added, "ledger_updated": updated,
               "corpus": str(corpus), "report": str(report), "stats": st, "telegram": sent}
    if as_json:
        _print_json(payload)
    else:
        ch.success(f"{len(found)} thread(s) where you replied to `{folder}` mail since {since}: "
                   f"{payload['confirmed']} confirmed, {payload['likely']} likely, "
                   f"{payload['answered_back']} answered back"
                   + (f", {labelled} labelled `{label}`" if apply_labels else ""))
        t = Table(title=f"{ledger_name}")
        for col in ("Date", "Tier", "Sender", "Subject", "Answered"):
            t.add_column(col, overflow="fold")
        for rt in found[:60]:
            t.add_row(rt.my_reply_at[:10], rt.tier, (rt.sender_name or rt.sender)[:30], rt.subject[:50],
                      f"oui ({rt.answer_at[:10]})" if rt.answered_back else "—")
        ch.console.print(t)
        ch.dim(f"Ledger → {ledger_path} (+{added}, ~{updated}) · corpus → {corpus} · stats → {report}")
        if sent is not None:
            ch.dim("Telegram: sent." if sent else "Telegram: FAILED")
    if sent is False:
        raise typer.Exit(1)


@email_app.command("followup")
def cmd_followup(
    space: Annotated[str, typer.Option("--space", help=_SPACE_HELP)],
    ledger_name: Annotated[str, typer.Option("--ledger")] = "piratebay",
    style: Annotated[Path | None, typer.Option("--style", help="Style prompt file (default: space config mailroom.style).")] = None,
    draft: Annotated[bool, typer.Option("--draft", help="Create Gmail drafts (never sends).")] = False,
    only_answered: Annotated[bool, typer.Option("--only-answered/--all", help="Only threads where they answered back.")] = True,
    model: Annotated[str | None, typer.Option("--model", help="provider:model, e.g. ollama:llama3")] = None,
    allow_cloud: Annotated[bool, typer.Option("--allow-cloud")] = False,
    limit: Annotated[int | None, typer.Option("--limit")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Generate previews, save no drafts.")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Draft follow-ups in the space's style for ledger threads (opt-in model; drafts only)."""
    from navig_email import account, followup as F, ledger as L, llm_guard

    paths = _paths(space)
    if not (draft or dry_run):
        ch.error("say what to do: --draft (Gmail drafts) or --dry-run (previews)")
        raise typer.Exit(2)
    style_path = style or paths.style_path
    if not style_path or not Path(style_path).exists():
        ch.error("no style prompt: pass --style <file> or set mailroom.style in the space config")
        raise typer.Exit(2)
    style_text = Path(style_path).read_text(encoding="utf-8")
    rows = L.read_jsonl(paths.ledger(ledger_name))
    if not rows:
        ch.info(f"ledger {ledger_name} is empty — run `{CMD} replied` first")
        return
    c = _connector()
    my_email = account.connected_email() or _run(account.profile(c))["email"]
    model, allow_cloud = _ai_defaults(paths, model, allow_cloud)
    try:
        res = _run(F.draft_followups(c, rows, style_text=style_text, my_email=my_email,
                                     only_answered=only_answered, model=model, allow_cloud=allow_cloud,
                                     limit=limit, dry_run=dry_run))
    except llm_guard.CloudRefused as exc:
        ch.error(str(exc))
        raise typer.Exit(1) from exc
    if not dry_run and res.drafted:
        L.upsert(paths.ledger(ledger_name), res.drafted, key="thread_id")
    payload = {"provider": res.provider, "drafted": len(res.drafted), "skipped": res.skipped,
               "errors": res.errors, "dry_run": dry_run,
               "previews": [{"thread_id": r.get("thread_id"), "sender": r.get("sender"),
                             "preview": r.get("draft_preview", "")} for r in res.drafted] if dry_run else []}
    if as_json:
        _print_json(payload)
    else:
        ch.info(f"Modèle : {res.provider}")
        verb = "Previewed" if dry_run else "Drafted"
        ch.success(f"{verb} {len(res.drafted)} · skipped {res.skipped}")
        for r in res.drafted[:10] if dry_run else []:
            ch.console.rule(f"{r.get('sender')} · {r.get('subject')}")
            ch.console.print(r.get("draft_preview", ""))
        for e in res.errors[:20]:
            ch.warning(e)
    if res.errors:
        raise typer.Exit(1)
