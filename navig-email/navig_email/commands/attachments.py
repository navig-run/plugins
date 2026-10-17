"""navig email attachments — hand an email's documents to the space's paperwork intake."""

from __future__ import annotations

from typing import Annotated

import typer

from navig_sdk import console as ch

attachments_app = typer.Typer(help="📎 Email documents → the paperwork intake (inbox/), provenance kept.",
                              no_args_is_help=True)


@attachments_app.command("save")
def cmd_save(
    message: Annotated[str, typer.Argument(help="A Gmail message id.")],
    space: Annotated[str, typer.Option("--space", help="Space name or path.")],
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Save the message's documents into <space>/inbox/ for paperwork to file (local only)."""
    from navig_email import account, attachments as A
    from navig_email.space import paths_for

    try:
        paths = paths_for(space)
    except ValueError as exc:
        ch.error(str(exc))
        raise typer.Exit(2) from exc

    async def _go():
        c = await account.connector()
        return await A.save_from_message(c, message, paths=paths, account=account.connected_email(), dry_run=dry_run)

    try:
        res = account.run(_go())
    except account.NotConnected as exc:
        ch.error(str(exc))
        raise typer.Exit(1) from exc
    for row in res.saved:
        ch.success(f"{'Would save' if dry_run else 'Saved'} {row['filename']} → {row['saved_as']}")
    for s in res.skipped:
        ch.dim(f"   skipped {s}")
    for e in res.errors:
        ch.error(e)
    if not res.saved and not res.skipped:
        ch.info("No attachments in that message.")
    elif res.saved and not dry_run:
        ch.dim("navig paperwork scan files them on its next pass (or run `navig paperwork scan inbox --space …`).")
    if res.errors:
        raise typer.Exit(1)
