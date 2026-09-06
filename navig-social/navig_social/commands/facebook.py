"""NAVIG Facebook Page CLI — manage a Page you administer.

The Studio publisher can only *post* to a Facebook Page. This adds the
*management* surface — back up every photo, edit page info + captions, and
delete photos — reusing the same vault token (``facebook``) + page-id config the
publisher already uses. Destructive ops are backup-gated and confirm-gated.

  navig facebook check                      verify token + show the Page
  navig facebook info                       show about / description / category
  navig facebook set-about --description …   edit page info
  navig facebook photos                     count + preview uploaded photos
  navig facebook backup [--out DIR]         download EVERY photo + a manifest
  navig facebook caption --id … --text …    edit one photo's caption
  navig facebook delete --id …              delete one photo
  navig facebook delete-all [--dry-run]     delete every backed-up photo
"""
from __future__ import annotations

import asyncio
from typing import Annotated

import typer

from navig.lazy_loader import lazy_import

ch = lazy_import("navig.console_helper")

facebook_app = typer.Typer(
    name="facebook",
    help="📘 Manage a Facebook Page — backup / edit / delete photos + page info",
    no_args_is_help=True,
)

_CONNECT_HELP = (
    "Set your Page access token + id, then retry:\n"
    "  navig vault set facebook <PAGE_ACCESS_TOKEN>\n"
    "  navig config set adapters.social.facebook.page_id <PAGE_ID>\n"
    "(env NAVIG_FACEBOOK_TOKEN / NAVIG_FACEBOOK_PAGE_ID also work, or use the\n"
    "deck Settings → Facebook.) Get a Page token from Graph API Explorer with:\n"
    "  pages_show_list, pages_read_engagement, pages_manage_metadata, pages_manage_posts"
)


def _admin():
    """Build a configured admin client or exit with guidance."""
    from navig_social.social.facebook_admin import FacebookPageAdmin

    admin = FacebookPageAdmin()
    if not admin.is_configured():
        ch.error("Facebook Page not connected.", _CONNECT_HELP)
        raise typer.Exit(1)
    return admin


def _run(coro):
    """Run an async engine call, mapping FacebookAdminError to a clean CLI error."""
    from navig_social.social.facebook_admin import FacebookAdminError

    try:
        return asyncio.run(coro)
    except FacebookAdminError as exc:
        detail = None
        if exc.code:
            detail = f"code={exc.code} http={exc.http_status}"
        ch.error(str(exc), detail)
        raise typer.Exit(1) from None


# ── read ──────────────────────────────────────────────────────────────────────


@facebook_app.command("check")
def facebook_check() -> None:
    """Verify the token works and show which Page it controls."""
    admin = _admin()
    data = _run(admin.get_page_info("name,id,category,fan_count"))
    ch.success(f"Connected → {data.get('name')}")
    ch.console.print(f"  [dim]id[/dim]        {data.get('id')}")
    if data.get("category"):
        ch.console.print(f"  [dim]category[/dim]  {data.get('category')}")
    if data.get("fan_count") is not None:
        ch.console.print(f"  [dim]followers[/dim] {data.get('fan_count'):,}")


@facebook_app.command("info")
def facebook_info() -> None:
    """Show the Page's current about / description / category / contact fields."""
    admin = _admin()
    data = _run(admin.get_page_info())
    for key in ("name", "category", "about", "description", "general_info", "website", "phone"):
        if data.get(key):
            ch.console.print(f"[bold]{key:12}[/bold] {data[key]}")
    if data.get("emails"):
        ch.console.print(f"[bold]{'emails':12}[/bold] {', '.join(data['emails'])}")


@facebook_app.command("photos")
def facebook_photos(
    limit: Annotated[int, typer.Option("--limit", "-n", help="Only fetch first N")] = 0,
) -> None:
    """Count and preview the Page's uploaded photos."""
    admin = _admin()
    ch.info("Fetching photo list …")
    items = _run(admin.list_photos(limit=limit or None))
    ch.success(f"{len(items)} uploaded photo(s).")
    for it in items[:10]:
        cap = (it.get("name") or "").replace("\n", " ")
        ch.console.print(f"  [dim]{it['id']}[/dim]  {it.get('created_time', '')}  {cap[:50]}")


# ── edit ──────────────────────────────────────────────────────────────────────


@facebook_app.command("set-about")
def facebook_set_about(
    about: Annotated[str, typer.Option("--about", help="Short 'about' text")] = "",
    description: Annotated[str, typer.Option("--description", help="Long description")] = "",
    category: Annotated[str, typer.Option("--category", help="Page category")] = "",
    about_file: Annotated[str, typer.Option("--about-file", help="Read about from a file")] = "",
    description_file: Annotated[str, typer.Option("--description-file", help="Read description from a file")] = "",
) -> None:
    """Update the Page's about / description / category text."""
    from pathlib import Path

    admin = _admin()
    if about_file:
        about = Path(about_file).read_text(encoding="utf-8").strip()
    if description_file:
        description = Path(description_file).read_text(encoding="utf-8").strip()
    fields = {
        "about": about or None,
        "description": description or None,
        "category": category or None,
    }
    if not any(fields.values()):
        ch.error("Nothing to change.", "Pass --about / --description / --category (or the *-file variants).")
        raise typer.Exit(1)
    _run(admin.set_page_info(**fields))
    ch.success("Updated. Run 'navig facebook info' to confirm.")


@facebook_app.command("caption")
def facebook_caption(
    photo_id: Annotated[str, typer.Option("--id", help="Photo id (from 'photos')")],
    text: Annotated[str, typer.Option("--text", help="New caption")],
) -> None:
    """Edit one photo's caption (best-effort; some photos reject edits)."""
    admin = _admin()
    resp = _run(admin.set_caption(photo_id, text))
    if resp is True or resp == {} or (isinstance(resp, dict) and resp.get("success")):
        ch.success("Caption updated.")
    else:
        ch.warning("Facebook did not confirm the edit — raw response below.")
        ch.print_json(resp if isinstance(resp, (dict, list)) else {"response": str(resp)})


# ── backup ────────────────────────────────────────────────────────────────────


@facebook_app.command("backup")
def facebook_backup(
    out: Annotated[str, typer.Option("--out", "-o", help="Backup directory")] = "facebook-backup",
    limit: Annotated[int, typer.Option("--limit", "-n", help="Only first N (for testing)")] = 0,
    overwrite: Annotated[bool, typer.Option("--overwrite", help="Re-download existing files")] = False,
) -> None:
    """Download EVERY uploaded photo at full resolution + a manifest. Do this before deleting."""
    admin = _admin()
    ch.info(f"Backing up photos → {out}/ …")

    def _progress(n: int, pid: str) -> None:
        if n % 25 == 0:
            ch.console.print(f"  [dim]… {n} processed[/dim]")

    res = _run(admin.backup_photos(out, limit=limit or None, overwrite=overwrite, on_progress=_progress))
    ch.success(f"Backed up {res['total']} photo(s): "
               f"{res['downloaded']} downloaded, {res['skipped']} already-had, {res['failed']} failed.")
    ch.console.print(f"  [dim]images[/dim]   {res['photos_dir']}")
    ch.console.print(f"  [dim]manifest[/dim] {res['manifest']}")
    if res["failed"]:
        ch.warning("Some photos failed — do NOT run delete-all until every photo is backed up.")


# ── delete ────────────────────────────────────────────────────────────────────


@facebook_app.command("delete")
def facebook_delete(
    photo_id: Annotated[str, typer.Option("--id", help="Photo id to delete")],
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation")] = False,
) -> None:
    """Delete a single photo (permanent)."""
    admin = _admin()
    if not yes and not typer.confirm(f"Permanently delete photo {photo_id}?"):
        ch.warning("Aborted.")
        raise typer.Exit(0)
    _run(admin.delete_photo(photo_id))
    ch.success(f"Deleted {photo_id}.")


@facebook_app.command("delete-all")
def facebook_delete_all(
    out: Annotated[str, typer.Option("--out", "-o", help="Backup dir to verify against")] = "facebook-backup",
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be deleted, change nothing")] = False,
    allow_missing: Annotated[bool, typer.Option("--allow-missing", help="Proceed even if some backups are missing")] = False,
    delay: Annotated[float, typer.Option("--delay", help="Seconds between deletions")] = 1.0,
    yes: Annotated[bool, typer.Option("--yes", help="Skip the typed confirmation")] = False,
) -> None:
    """Delete every uploaded photo that is already backed up (never un-backed ones)."""
    admin = _admin()
    ch.info("Checking backup vs live photos …")
    plan = _run(admin.plan_delete_all(out))
    ch.console.print(f"  live uploaded photos:      {plan['live']}")
    ch.console.print(f"  [green]backed-up & deletable:[/green]     {len(plan['deletable'])}")
    ch.console.print(f"  [yellow]NOT backed up (skipped):[/yellow]   {len(plan['unbacked'])}")
    if plan["missing"]:
        ch.console.print(f"  [red]manifest photos missing files:[/red] {len(plan['missing'])}")

    if dry_run:
        for pid in plan["deletable"]:
            ch.console.print(f"  [dim]would delete {pid}[/dim]")
        ch.info("Dry run — no changes made.")
        raise typer.Exit(0)

    if plan["missing"] and not allow_missing:
        ch.error(f"{len(plan['missing'])} photo(s) in the manifest have no local file.",
                 "Re-run 'backup', or pass --allow-missing to skip them and proceed.")
        raise typer.Exit(1)
    if not plan["deletable"]:
        ch.warning("Nothing to delete.")
        raise typer.Exit(0)

    n = len(plan["deletable"])
    if not yes:
        phrase = typer.prompt(f"\nThis permanently deletes {n} photos. Type 'DELETE ALL PHOTOS' to proceed")
        if phrase.strip() != "DELETE ALL PHOTOS":
            ch.warning("Aborted — confirmation phrase did not match.")
            raise typer.Exit(0)

    def _on_delete(i: int, total: int, pid: str) -> None:
        ch.console.print(f"  [{i}/{total}] deleted {pid}")

    res = _run(admin.run_deletions(plan["deletable"], delay=delay, on_delete=_on_delete))
    ch.success(f"Deleted {res['deleted']}, failed {res['failed']}. "
               f"Skipped {len(plan['unbacked'])} un-backed-up photo(s).")


# alias target so `navig fb …` maps here via the registered entry point
fb_app = facebook_app
