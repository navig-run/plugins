"""navig email policy — which incoming mail the mailroom may answer on its own.

Every policy ships disabled. ``enable`` scopes one to an account (and optionally to aliases and
senders); from then on the 5-minute ``watch`` drafts or queues replies it covers. A queued
automatic reply waits its delay (default 15 min) and stays cancellable; ``outbox pause`` stops
them all. Contracts, payments, government mail, credentials and the like are never answered
automatically, whatever a policy says — they are drafted for you instead.
"""

from __future__ import annotations

import json as _json
from typing import Annotated, Any

import typer
from rich.table import Table

from navig_sdk import console as ch
from navig_sdk.host import command_name

CMD = command_name("email")

policy_app = typer.Typer(
    help="🤖 Auto-reply policies: list, enable for an account/alias, disable, test against a real message.",
    no_args_is_help=True,
)


def _P():
    from navig_email import policy

    return policy


def _print_json(payload: Any) -> None:
    ch.console.print_json(_json.dumps(payload, default=str))


@policy_app.command("list")
def cmd_list(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Every policy, its scope, and whether it is on."""
    from navig_email import outbox as ob

    pols = _P().all_policies()
    if as_json:
        _print_json({"policies": [{"enabled": p.enabled, **p.spec()} for p in pols], "pause": ob.pause_level()})
        return
    if ob.pause_level():
        ch.warning(f"Sending is paused ({ob.pause_level()}) — `{CMD} outbox resume`")
    t = Table(box=None, show_header=True, padding=(0, 2))
    t.add_column("Policy", no_wrap=True)
    t.add_column("State", no_wrap=True)
    t.add_column("Does", no_wrap=True)
    t.add_column("Account / aliases")
    t.add_column("Next step", no_wrap=True)
    for p in pols:
        state = "[green]● on[/green]" if p.enabled else "[dim]○ off[/dim]"
        does = "drafts only" if p.action == "draft" else f"sends after {int(p.delay.total_seconds() // 60)} min"
        scope = (p.account + (" · " + ", ".join(p.aliases) if p.aliases else "")) if p.enabled else "—"
        t.add_row(p.id, state, does, scope, "disable" if p.enabled else "enable --account …")
    ch.console.print(t)
    ch.dim(f"Details: `{CMD} policy show <id>` · try one on a real message: `{CMD} policy test <message-id> --space <space>`")


@policy_app.command("show")
def cmd_show(policy_id: Annotated[str, typer.Argument()], as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """One policy in full."""
    _P().install_templates()
    pol = _P().get(policy_id)
    if pol is None:
        ch.error(f"no policy {policy_id!r}")
        raise typer.Exit(1)
    if as_json:
        _print_json({"enabled": pol.enabled, **pol.spec()})
        return
    ch.console.rule(f"{pol.id} · {'on' if pol.enabled else 'off'}")
    for k, v in {"enabled": pol.enabled, **pol.spec()}.items():
        ch.console.print(f"  [bold]{k:<16}[/bold] {v}")


@policy_app.command("enable")
def cmd_enable(
    policy_id: Annotated[str, typer.Argument()],
    account: Annotated[str, typer.Option("--account", "-A", help="The mailbox this policy answers for.")],
    alias: Annotated[list[str] | None, typer.Option("--alias", help="Only mail sent to this alias (repeatable).")] = None,
    sender: Annotated[list[str] | None, typer.Option("--sender", help="Only these senders/domains (repeatable).")] = None,
    fact: Annotated[list[str] | None, typer.Option("--fact", help="A fact the reply may state (repeatable).")] = None,
    delay: Annotated[str | None, typer.Option("--delay", help="Cancellation window before sending (default 15m).")] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Required: this lets the mailroom send mail on its own.")] = False,
) -> None:
    """Turn a policy on for one account. Sending policies need --yes."""
    P = _P()
    P.install_templates()
    pol = P.get(policy_id)
    if pol is None:
        ch.error(f"no policy {policy_id!r} — `{CMD} policy list`")
        raise typer.Exit(1)
    if pol.action == "send" and not yes:
        ch.error(f"{pol.id} SENDS mail without asking (after a {int(pol.delay.total_seconds() // 60)}-minute "
                 "cancellable delay). Re-run with --yes to enable it.")
        raise typer.Exit(2)
    try:
        pol = P.enable(policy_id, account=account, aliases=alias, senders=sender, facts=fact, delay=delay,
                       actor="user:cli")
    except (P.PolicyError, ValueError) as exc:
        ch.error(str(exc))
        raise typer.Exit(2) from exc
    ch.success(f"{pol.id} is ON for {pol.account}" + (f" ({', '.join(pol.aliases)})" if pol.aliases else "")
               + (" — drafts only." if pol.action == "draft" else
                  f" — replies are queued and sent after {int(pol.delay.total_seconds() // 60)} min unless cancelled."))
    ch.dim(f"Emergency stop: `{CMD} outbox pause` · off: `{CMD} policy disable {pol.id}`")


@policy_app.command("disable")
def cmd_disable(policy_id: Annotated[str, typer.Argument()]) -> None:
    """Turn a policy off. Replies it already queued will not be sent."""
    P = _P()
    try:
        P.disable(policy_id, actor="user:cli")
    except P.PolicyError as exc:
        ch.error(str(exc))
        raise typer.Exit(1) from exc
    ch.success(f"{policy_id} is off. Its queued replies stop at dispatch (they move to review).")


@policy_app.command("test")
def cmd_test(
    message: Annotated[str, typer.Argument(help="A Gmail message id to evaluate (nothing is written or sent).")],
    space: Annotated[str, typer.Option("--space", help="Space name or path.")],
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """What the enabled policies would do with one real message — a dry run, no model call."""
    from navig_email import account, messages, senders
    from navig_email.space import paths_for

    P = _P()
    try:
        paths = paths_for(space)
    except ValueError as exc:
        ch.error(str(exc))
        raise typer.Exit(2) from exc

    async def _go():
        c = await account.connector()
        raw = await c.get_message(message, format="full")
        return messages.shape(raw, body=c._extract_body(raw.get("payload") or {}))

    try:
        msg = account.run(_go())
    except account.NotConnected as exc:
        ch.error(str(exc))
        raise typer.Exit(1) from exc
    me = account.connected_email()
    d = P.evaluate(msg, account=me, my_addresses=senders.authorized_senders(paths, me))
    from navig_email import safety

    payload = {"message": message, "outcome": d.outcome, "policy": d.policy_id, "category": d.category,
               "confidence": d.confidence, "reasons": d.reasons, "automated": safety.automated(msg),
               "authentication": safety.authentication(msg),
               "exclusions": safety.hard_exclusions(msg).categories}
    if as_json:
        _print_json(payload)
        return
    for k, v in payload.items():
        ch.console.print(f"  [bold]{k:<15}[/bold] {v}")
