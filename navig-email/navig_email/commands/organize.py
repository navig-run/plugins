"""navig email organize — audit Gmail labels and filters, preview a tidier set, migrate safely.

``audit`` and ``preview`` only read. ``apply`` is additive (new labels added, nothing removed)
and writes a snapshot first; ``rollback`` undoes it; ``retire`` takes the old labels off the
migrated messages once you have checked the result. System labels are never touched.
"""

from __future__ import annotations

import json as _json
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.table import Table

from navig_sdk import console as ch
from navig_sdk.host import command_name

CMD = command_name("email")
_SPACE_HELP = "Space name or absolute path (identities, and where snapshots are written)."

organize_app = typer.Typer(
    help="🗂 Labels & filters: audit, preview a taxonomy, apply additively (snapshot + rollback), retire old.",
    no_args_is_help=True,
)


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


def _print_json(payload: Any) -> None:
    ch.console.print_json(_json.dumps(payload, default=str, ensure_ascii=False))


def _audit_and_plan(space: str):
    from navig_email import account, organize as O

    paths = _paths(space)
    c = _connector()
    a = account.run(O.audit(c))
    return paths, c, a, O.preview(a, paths=paths)


@organize_app.command("audit")
def cmd_audit(
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Labels with counts, equivalent and unused labels, conflicting / forwarding filters (read-only)."""
    from navig_email import account, organize as O

    a = account.run(O.audit(_connector()))
    if as_json:
        _print_json(a.as_dict())
        return
    t = Table(box=None, show_header=True, padding=(0, 2))
    t.add_column("Label")
    t.add_column("Messages", no_wrap=True, justify="right")
    for l in sorted((l for l in a.labels if not l.system), key=lambda l: l.name.lower()):
        t.add_row(l.name, str(l.messages))
    ch.console.print(t)
    for title, rows in (("Equivalent labels", [" = ".join(g) for g in a.equivalent]), ("Unused labels", a.unused),
                        ("Conflicting filters", a.conflicts), ("Redundant filters", a.redundant),
                        ("Forwarding filters", a.forwarding), ("Filters pointing at missing labels", a.dangling)):
        if rows:
            ch.warning(f"{title}: {len(rows)}")
            for r in rows[:10]:
                ch.dim(f"   {r}")
    if a.filters_access != "ok":
        ch.info("Filters not read — `navig config set connectors.gmail.filters true`, then reconnect Gmail.")
    ch.dim(f"Next: `{CMD} organize preview --space <space>`")


@organize_app.command("preview")
def cmd_preview(
    space: Annotated[str, typer.Option("--space", help=_SPACE_HELP)],
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """The proposed taxonomy for THIS mailbox, with message counts. Changes nothing."""
    _paths_, _c, _a, plan = _audit_and_plan(space)
    if as_json:
        _print_json(plan.as_dict())
        return
    if not (plan.moves or plan.merges):
        ch.success("Nothing to reorganise.")
    t = Table(box=None, show_header=True, padding=(0, 2))
    t.add_column("From")
    t.add_column("To", no_wrap=True)
    t.add_column("Messages", justify="right", no_wrap=True)
    t.add_column("Why")
    for m in plan.moves:
        t.add_row(m.source, m.target, str(m.messages), m.why)
    for m in plan.merges:
        t.add_row(" + ".join(x for x in m["labels"] if x != m["into"]), m["into"], "", "equivalent names")
    ch.console.print(t)
    if plan.new_labels:
        ch.info("New labels: " + ", ".join(plan.new_labels))
    if plan.filters:
        ch.info(f"{len(plan.filters)} new filter(s) for future mail (old filters stay until you retire them)")
    for n in plan.notes:
        ch.warning(n)
    ch.dim(f"Apply (adds labels, removes nothing, snapshot first): `{CMD} organize apply --space {space} --yes`")


@organize_app.command("apply")
def cmd_apply(
    space: Annotated[str, typer.Option("--space", help=_SPACE_HELP)],
    yes: Annotated[bool, typer.Option("--yes", help="Required: this changes labels in your mailbox.")] = False,
) -> None:
    """Add the new labels to the messages (old labels kept), create new filters. Snapshot written first."""
    from navig_email import account, organize as O

    if not yes:
        ch.error("this changes your mailbox's labels (additively): add --yes after reading `organize preview`")
        raise typer.Exit(2)
    paths, c, a, plan = _audit_and_plan(space)
    out = account.run(O.apply(c, plan, a, snapshot_dir=paths.reports_dir))
    ch.success(f"Labelled: {sum(out['labelled'].values())} message(s); filters created: {out['filters_created']}")
    ch.info(f"Snapshot: {out['snapshot']}")
    ch.dim(f"Undo: `{CMD} organize rollback {out['snapshot']}` · once checked: `{CMD} organize retire --space {space} --yes`")


@organize_app.command("rollback")
def cmd_rollback(snapshot: Annotated[str, typer.Argument(help="The snapshot file `apply` printed.")]) -> None:
    """Undo an apply: take off the labels it added, delete the filters it created."""
    from navig_email import account, organize as O

    p = Path(snapshot).expanduser()
    if not p.is_file():
        ch.error(f"no snapshot at {p}")
        raise typer.Exit(2)
    out = account.run(O.rollback(_connector(), p))
    ch.success(f"Rolled back: {out['labels_removed']} label change(s), {out['filters_deleted']} filter(s).")


@organize_app.command("retire")
def cmd_retire(
    space: Annotated[str, typer.Option("--space", help=_SPACE_HELP)],
    yes: Annotated[bool, typer.Option("--yes", help="Required: this takes old labels off migrated messages.")] = False,
) -> None:
    """After checking `apply`: take the OLD labels off migrated messages (labels and filters are kept)."""
    from navig_email import account, organize as O

    if not yes:
        ch.error("this removes the old labels from migrated messages: add --yes once you checked the result")
        raise typer.Exit(2)
    _paths_, c, a, plan = _audit_and_plan(space)
    out = account.run(O.retire(c, plan, a))
    ch.success(f"Old labels taken off {out['labels_taken_off']} message(s).")
