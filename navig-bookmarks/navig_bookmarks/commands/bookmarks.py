"""``navig bookmarks`` / ``navig-bookmarks`` — the bookmark manager's command surface.

One Typer app, two front doors: navig mounts it as ``navig bookmarks`` (the
``navig.commands`` entry point), and the ``navig-bookmarks`` console script runs it with no
navig installed. ``navig bookmarks <url>`` is short for ``navig bookmarks add <url>``.

Exit codes follow navig's plugin contract: 2 for usage (unknown id, bad argument, no AI
configured), 1 for an operation that failed, 0 otherwise — a dead link found by ``check`` is
a finding, not a failure, unless ``--fail-on-dead`` asks for it to be one.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer
from typer.core import TyperGroup

from navig_sdk import console as ch
from navig_sdk.host import command_name

from navig_bookmarks import __version__
from navig_bookmarks._typed_path import resolve_user_path
from navig_bookmarks.models import Bookmark, join_folder, split_folder
from navig_bookmarks.urls import ensure_scheme, looks_like_url, site_of

CMD = command_name("bookmarks")


class _UrlShortcutGroup(TyperGroup):
    """``bookmarks https://example.com`` means ``bookmarks add https://example.com``."""

    def parse_args(self, ctx, args):  # noqa: ANN001, ANN201
        if args and args[0] not in self.commands and not args[0].startswith("-") and looks_like_url(args[0]):
            args = ["add", *args]
        return super().parse_args(ctx, args)


app = typer.Typer(
    name="bookmarks",
    cls=_UrlShortcutGroup,
    help="🔖  Your bookmarks: import, search, organize into folders, find dead links and duplicates.",
    no_args_is_help=True,
)
rules_app = typer.Typer(name="rules", help="Offline tagging rules: pattern -> tags (no AI).", no_args_is_help=True)
app.add_typer(rules_app, name="rules")


# ── helpers ─────────────────────────────────────────────────────────────────────

_STORES: dict[str, object] = {}


def _store():
    """The store for this process; the first open also copies new rows from navig links."""
    from navig_bookmarks.migrate import migrate_legacy_links  # noqa: PLC0415
    from navig_bookmarks.store import BookmarkStore, default_db_path  # noqa: PLC0415

    path = default_db_path()
    key = str(path)
    if key in _STORES:
        return _STORES[key]
    try:
        store = BookmarkStore(path)
    except sqlite3.DatabaseError as exc:
        ch.error(f"Cannot open the bookmark store at {path}", str(exc))
        raise typer.Exit(1) from exc
    if os.environ.get("NAVIG_BOOKMARKS_NO_MIGRATE") != "1":
        try:
            res = migrate_legacy_links(store)
        except (sqlite3.DatabaseError, OSError) as exc:
            typer.echo(f"note: could not read the old navig links store ({exc}); skipped copying it", err=True)
        else:
            if res.copied:
                typer.echo(
                    f"note: copied {res.copied} bookmark(s) from navig links ({res.source}); "
                    "that file is left untouched.",
                    err=True,
                )
    _STORES[key] = store
    return store


def _get(store, ref: str) -> Bookmark:
    bm = store.resolve_id(ref)
    if bm is None:
        ch.error(f"No bookmark {ref!r}", f"find one with: {CMD} search <words>")
        raise typer.Exit(2)
    return bm


def _split_csv(value: Optional[str]) -> list[str]:
    return [t.strip() for t in (value or "").split(",") if t.strip()]


def _folder_arg(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return join_folder([p for p in value.split("/")])


def _table(*columns: tuple[str, dict]):
    from rich.table import Table  # noqa: PLC0415

    table = Table(box=None, show_header=True, padding=(0, 2), header_style="bold")
    for name, kw in columns:
        table.add_column(name, **kw)
    return table


def _print(renderable) -> None:  # noqa: ANN001
    ch.console.print(renderable)


def _emit_json(data) -> None:  # noqa: ANN001
    typer.echo(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def _folder_label(path: str) -> str:
    return " / ".join(split_folder(path)) or "—"


def _epoch(stamp: Optional[str]) -> Optional[int]:
    if not stamp:
        return None
    text = stamp.strip().replace("Z", "+00:00").replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _bookmark_rows(bookmarks: list[Bookmark]):
    table = _table(
        ("ID", {"style": "cyan", "no_wrap": True}),
        ("Title", {"overflow": "fold"}),
        ("Folder", {"style": "magenta", "no_wrap": True, "max_width": 32}),
        ("Tags", {"style": "yellow", "no_wrap": True, "max_width": 28}),
        ("Site", {"style": "blue", "no_wrap": True, "max_width": 28}),
    )
    for b in bookmarks:
        table.add_row(
            b.id,
            b.title or b.url,
            _folder_label(b.folder_path),
            ", ".join(b.tag_list) or "—",
            site_of(b.url) or b.url[:28],
        )
    return table


def _generator(provider: Optional[str], model: Optional[str]):
    from navig_sdk import ai  # noqa: PLC0415

    def _gen(system: str, user: str, max_tokens: int) -> str:
        return ai.generate(
            user, system=system, max_tokens=max_tokens, temperature=0.2, timeout=180,
            provider=provider, model=model,
        )

    return _gen


def _run_ai(fn, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
    """Call an AI-backed planner; map 'no AI' to exit 2 and any AI failure to exit 1."""
    from navig_sdk import ai  # noqa: PLC0415

    try:
        return fn(*args, **kwargs)
    except typer.Exit:
        raise
    except ai.AINotConfigured as exc:
        ch.error("This needs an AI, and none is set up.", str(exc))
        raise typer.Exit(2) from exc
    except Exception as exc:  # noqa: BLE001 - provider errors come in many types; all are failures
        ch.error("The AI request failed.", f"{type(exc).__name__}: {exc}")
        raise typer.Exit(1) from exc


def _progress(label: str):
    def _p(n: int, total: int) -> None:
        if total > 1 and sys.stderr.isatty():
            typer.echo(f"  {label} {n}/{total}…", err=True)

    return _p


# ── add / list / search / show ──────────────────────────────────────────────────


@app.command("add")
def add_cmd(
    url: str = typer.Argument(..., help="URL to save (https:// may be left out)."),
    title: Optional[str] = typer.Option(None, "--title", "-t", help="Title."),
    notes: Optional[str] = typer.Option(None, "--notes", "-n", help="A note to find it by later."),
    tags: Optional[str] = typer.Option(None, "--tags", "-T", help="Comma-separated tags."),
    folder: Optional[str] = typer.Option(None, "--folder", "-f", help='Folder, levels separated by "/".'),
    cred: Optional[str] = typer.Option(None, "--cred", "-c", help="Vault credential id for auto-login on open."),
    json_output: bool = typer.Option(False, "--json", help="Print the result as JSON."),
):
    """Save a bookmark."""
    from navig_bookmarks.rules import tagger  # noqa: PLC0415

    if not looks_like_url(url):
        ch.error(f"{url!r} does not look like a URL")
        raise typer.Exit(2)
    url = ensure_scheme(url)
    store = _store()
    dup = store.find_duplicate(url)
    if dup is not None:
        if json_output:
            _emit_json({"id": dup.id, "url": dup.url, "duplicate": True})
        else:
            ch.warning(f"Already saved as {dup.id}", f"edit it with: {CMD} edit {dup.id}")
        return
    bid = store.add(url, title=title, notes=notes, folder=_folder_arg(folder) or "", tags=_split_csv(tags),
                    vault_cred_id=cred)
    auto = tagger(store.rules())
    rule_tags = auto(url) if auto else []
    if rule_tags:
        store.add_tags(bid, rule_tags, source="rule")
    if json_output:
        _emit_json({"id": bid, "url": url, "duplicate": False, "rule_tags": rule_tags})
        return
    ch.success(f"Saved {bid}  {url}")
    if rule_tags:
        ch.dim(f"  rule tags: {', '.join(rule_tags)}")


@app.command("list")
def list_cmd(
    tag: Optional[str] = typer.Option(None, "--tag", "-t", help="Only this tag."),
    folder: Optional[str] = typer.Option(None, "--folder", "-f", help="Only this folder (and below)."),
    cred: Optional[str] = typer.Option(None, "--cred", "-c", help="Only bookmarks using this credential."),
    untagged: bool = typer.Option(False, "--untagged", help="Only bookmarks with no tags."),
    limit: int = typer.Option(50, "--limit", "-n", help="At most this many."),
    json_output: bool = typer.Option(False, "--json", help="Print as JSON."),
):
    """List bookmarks, most recently used first."""
    store = _store()
    rows = store.list_bookmarks(tag=tag, folder=_folder_arg(folder), cred=cred, untagged=untagged, limit=limit)
    if json_output:
        _emit_json([b.to_dict() for b in rows])
        return
    if not rows:
        ch.info(f"No bookmarks yet · add one with {CMD} <url>, or {CMD} import <file>")
        return
    _print(_bookmark_rows(rows))
    total = store.count()
    ch.dim(f"{len(rows)} of {total} · search with {CMD} search <words>")


@app.command("search")
def search_cmd(
    query: str = typer.Argument(..., help="Words to find in title, URL, notes, tags or folder."),
    limit: int = typer.Option(20, "--limit", "-n", help="At most this many."),
    json_output: bool = typer.Option(False, "--json", help="Print as JSON."),
):
    """Full-text search."""
    rows = _store().search(query, limit=limit)
    if json_output:
        _emit_json([b.to_dict() for b in rows])
        return
    if not rows:
        ch.info(f"Nothing matches {query!r}")
        return
    _print(_bookmark_rows(rows))
    ch.dim(f"{len(rows)} result(s) · open one with {CMD} open <id>")


@app.command("show")
def show_cmd(
    ref: str = typer.Argument(..., help="Bookmark id (a unique prefix is enough)."),
    json_output: bool = typer.Option(False, "--json", help="Print as JSON."),
):
    """Everything about one bookmark."""
    bm = _get(_store(), ref)
    if json_output:
        _emit_json(bm.to_dict())
        return
    table = _table(("Field", {"style": "dim", "no_wrap": True}), ("Value", {"overflow": "fold"}))
    tags = ", ".join(f"{t} [dim]({s})[/dim]" if s != "user" else t for t, s in sorted(bm.tags.items()))
    check = "—"
    if bm.checked_at:
        check = f"{bm.http_status or bm.check_error or '?'} at {bm.checked_at[:19]}"
        if bm.final_url:
            check += f" → {bm.final_url}"
    for field, value in (
        ("ID", bm.id), ("URL", bm.url), ("Title", bm.title or "—"), ("Folder", _folder_label(bm.folder_path)),
        ("Tags", tags or "—"), ("Notes", bm.notes or "—"), ("Credential", bm.vault_cred_id or "—"),
        ("Visits", str(bm.visit_count)), ("Last visit", (bm.last_visited or "—")[:19]),
        ("Last check", check), ("Added", (bm.created_at or "—")[:19]),
    ):
        table.add_row(field, value)
    _print(table)


# ── open ────────────────────────────────────────────────────────────────────────


def _open_plain(url: str) -> None:
    import webbrowser  # noqa: PLC0415

    webbrowser.open(url)


@app.command("open")
def open_cmd(
    ref: str = typer.Argument(..., help="Bookmark id."),
    profile: Optional[str] = typer.Option(
        None, "--profile", "-p", help="Named browser profile that keeps the login (navig only)."
    ),
    headless: bool = typer.Option(False, "--headless", help="Log in without a window, then close it (navig only)."),
):
    """Open a bookmark. With a vault credential and navig installed, the login is filled in."""
    import asyncio  # noqa: PLC0415

    store = _store()
    bm = _get(store, ref)
    store.record_visit(bm.id)
    if not bm.vault_cred_id:
        if headless:
            ch.warning("No credential is attached to this bookmark, so --headless has nothing to do.")
            raise typer.Exit(2)
        ch.info(f"Opening {bm.url}")
        _open_plain(bm.url)
        return
    try:
        from navig.browser import cdp_actions  # noqa: PLC0415 - navig's browser, only when navig is here
    except ImportError:
        if headless:
            ch.warning("Auto-login needs navig (pip install navig); --headless has nothing to do without it.")
            raise typer.Exit(2) from None
        ch.warning("Auto-login needs navig (pip install navig) — opening without it.")
        _open_plain(bm.url)
        return

    ch.info(f"Opening {bm.url} with auto-login (credential {bm.vault_cred_id})")
    launched = cdp_actions.new(app="chrome", profile=profile, headless=headless, context="human")
    if not launched.get("ok"):
        ch.warning(f"Could not start a browser for auto-login ({launched.get('error', 'unknown')}); opening without it.")
        _open_plain(bm.url)
        return
    port = launched["port"]
    try:
        result = asyncio.run(cdp_actions.login(port, open_url=bm.url))
    except Exception as exc:  # noqa: BLE001 - the page is open either way; report, don't crash
        result = {"status": "error", "error": str(exc)}
    status = result.get("status") or ("ok" if result.get("ok") else "error")
    if status in ("ok", "submitted", "filled"):
        ch.success(f"Logged in on port {port}.")
    elif status == "no_credential":
        ch.warning(f"No vault login found for {bm.url}. Add one with 'navig vault login add', or fix --cred.")
    else:
        ch.warning(f"Auto-login did not complete ({status}: {result.get('error', '')}).")
    if headless:
        cdp_actions.stop(port=port)
        ch.info("Headless session closed" + (f"; profile '{profile}' keeps the login." if profile else "."))
    else:
        ch.info(f"Browser is open on port {port} — close it when done, or 'navig cdp stop --port {port}'.")


# ── edit / tag / delete ─────────────────────────────────────────────────────────


@app.command("edit")
def edit_cmd(
    ref: str = typer.Argument(..., help="Bookmark id."),
    url: Optional[str] = typer.Option(None, "--url", help="New URL."),
    title: Optional[str] = typer.Option(None, "--title", "-t"),
    notes: Optional[str] = typer.Option(None, "--notes", "-n"),
    tags: Optional[str] = typer.Option(None, "--tags", "-T", help="Comma-separated; replaces YOUR tags (AI/rule tags stay)."),
    folder: Optional[str] = typer.Option(None, "--folder", "-f", help='Folder ("" for none).'),
    cred: Optional[str] = typer.Option(None, "--cred", "-c", help='Vault credential id ("" to detach).'),
):
    """Change a bookmark."""
    store = _store()
    bm = _get(store, ref)
    if url is not None:
        if not looks_like_url(url):
            ch.error(f"{url!r} does not look like a URL")
            raise typer.Exit(2)
        url = ensure_scheme(url)
    if not store.update(bm.id, url=url, title=title, notes=notes, folder=_folder_arg(folder), vault_cred_id=cred):
        ch.error(f"Could not update {bm.id}")
        raise typer.Exit(1)
    if tags is not None:
        wanted = _split_csv(tags)
        mine = [t for t, s in bm.tags.items() if s == "user"]
        store.remove_tags(bm.id, [t for t in mine if t not in wanted])
        store.add_tags(bm.id, wanted, source="user")
    ch.success(f"Updated {bm.id}")


@app.command("tag")
def tag_cmd(
    args: Optional[list[str]] = typer.Argument(None, help="ID TAG... to add tags; with --ai: the IDs to tag."),
    ai_tags: bool = typer.Option(False, "--ai", help="Let the AI suggest tags."),
    remove: bool = typer.Option(False, "--remove", "-r", help="Remove the given tags instead."),
    all_: bool = typer.Option(False, "--all", help="With --ai: every bookmark."),
    untagged: bool = typer.Option(False, "--untagged", help="With --ai: bookmarks that have no tags."),
    curated: bool = typer.Option(False, "--curated", help="With --ai: only use tags that already exist."),
    max_tags: int = typer.Option(4, "--max", help="With --ai: at most this many tags each."),
    dry_run: bool = typer.Option(False, "--dry-run", help="With --ai: show suggestions, change nothing."),
    provider: Optional[str] = typer.Option(None, "--provider", help="AI provider (e.g. openai, ollama, custom)."),
    model: Optional[str] = typer.Option(None, "--model", help="AI model."),
    json_output: bool = typer.Option(False, "--json", help="Print as JSON."),
):
    """Add or remove tags — or let the AI tag (it only ever replaces its own tags)."""
    store = _store()
    args = list(args or [])
    if not ai_tags:
        if len(args) < 2:
            ch.error(f"Usage: {CMD} tag ID TAG... [--remove]   or   {CMD} tag --ai [IDS...|--all|--untagged]")
            raise typer.Exit(2)
        bm = _get(store, args[0])
        if remove:
            n = store.remove_tags(bm.id, args[1:])
            ch.success(f"Removed {n} tag(s) from {bm.id}")
        else:
            store.add_tags(bm.id, args[1:], source="user")
            ch.success(f"Tagged {bm.id}: {', '.join(args[1:])}")
        return

    from navig_bookmarks.tagging import suggest_tags  # noqa: PLC0415

    if args:
        targets = [_get(store, ref) for ref in args]
    elif all_ or untagged:
        targets = store.list_bookmarks(untagged=untagged, order="insertion")
    else:
        ch.error(f"Say which bookmarks: {CMD} tag --ai ID... | --all | --untagged")
        raise typer.Exit(2)
    if not targets:
        ch.info("Nothing to tag.")
        return
    vocabulary = list(store.tag_counts())
    if curated and not vocabulary:
        ch.error("--curated picks only from existing tags, and there are none yet.")
        raise typer.Exit(2)
    result = _run_ai(
        suggest_tags, targets, _generator(provider, model), vocabulary=vocabulary, curated=curated,
        max_tags=max(1, min(max_tags, 8)), progress=_progress("tagging batch"),
    )
    changes = []
    for b in targets:
        if b.id not in result.tags:
            continue
        if dry_run:
            added = [t for t in result.tags[b.id] if t not in b.tags]
            removed = [t for t, s in b.tags.items() if s == "ai" and t not in result.tags[b.id]]
        else:
            added, removed = store.replace_source_tags(b.id, result.tags[b.id], "ai")
        changes.append((b, result.tags[b.id], added, removed))
    if json_output:
        _emit_json({
            "dry_run": dry_run,
            "tags": {b.id: tags for b, tags, _a, _r in changes},
            "missing": result.missing, "invented": result.invented, "errors": result.errors,
        })
    else:
        table = _table(
            ("ID", {"style": "cyan", "no_wrap": True}), ("Title", {"overflow": "fold"}),
            ("AI tags", {"style": "yellow"}), ("Removed", {"style": "dim"}),
        )
        for b, tags, added, removed in changes[:60]:
            shown = ", ".join(f"[bold]{t}[/bold]" if t in added else t for t in tags) or "—"
            table.add_row(b.id, b.title or b.url, shown, ", ".join(removed) or "—")
        _print(table)
        verb = "would change" if dry_run else "changed"
        ch.dim(f"{len(changes)} tagged, {verb} {sum(1 for c in changes if c[2] or c[3])} · {result.calls} AI call(s)")
    if result.missing:
        if not changes:
            ch.error(f"The AI tagged none of the {len(result.missing)} bookmark(s).", "; ".join(result.errors[:3]) or None)
            raise typer.Exit(1)
        ch.warning(f"{len(result.missing)} bookmark(s) got no answer from the AI; run again to retry them.")


@app.command("delete")
def delete_cmd(
    ref: str = typer.Argument(..., help="Bookmark id."),
    force: bool = typer.Option(False, "--force", "-f", help="Do not ask."),
):
    """Delete a bookmark."""
    store = _store()
    bm = _get(store, ref)
    if not force and not typer.confirm(f"Delete {bm.id} ({bm.url})?", default=False):
        raise typer.Abort()
    if not store.delete(bm.id):
        ch.error(f"Could not delete {bm.id}")
        raise typer.Exit(1)
    ch.success(f"Deleted {bm.id}")


# ── import / export ─────────────────────────────────────────────────────────────


@app.command("import")
def import_cmd(
    file: Optional[str] = typer.Argument(None, help="Export file or browser profile file (omit for a browser's default profile)."),
    source: str = typer.Option("auto", "--source", "-s", help="auto | html | json | chrome | edge | firefox | safari"),
    into: Optional[str] = typer.Option(None, "--into", help='Put everything under this folder (e.g. "Imported/Laptop").'),
    tag: Optional[str] = typer.Option(None, "--tag", help="Comma-separated tags to add to every imported bookmark."),
    cred: Optional[str] = typer.Option(None, "--cred", "-c", help="Vault credential id for every imported bookmark."),
    json_output: bool = typer.Option(False, "--json", help="Print the result as JSON."),
):
    """Import bookmarks — folders are kept; URLs already saved are skipped."""
    from navig_bookmarks import importers  # noqa: PLC0415
    from navig_bookmarks.rules import tagger  # noqa: PLC0415

    src = source.lower().strip()
    if src != "auto" and src not in importers.SOURCES:
        ch.error(f"Unknown source {source!r}", f"one of: auto, {', '.join(importers.SOURCES)}")
        raise typer.Exit(2)
    if file is None:
        if src not in importers.BROWSERS:
            ch.error("Give a file to import, or --source chrome|edge|firefox|safari for a browser's default profile.")
            raise typer.Exit(2)
        found = importers.default_path(src)
        if found is None:
            ch.error(f"No default {src} profile found on this computer.", "pass the bookmarks file instead")
            raise typer.Exit(2)
        path = found
    else:
        path = resolve_user_path(file)
        if not path.exists():
            ch.error(f"Not found: {file}")
            raise typer.Exit(2)
    try:
        if src == "auto":
            src = importers.detect_source(path)
        items = importers.read(src, path)
    except importers.ImportSourceError as exc:
        ch.error(f"Could not import {path.name}", str(exc))
        raise typer.Exit(1) from exc
    if into:
        importers.folder_prefix(items, into)
    extra = _split_csv(tag)
    if extra:
        for it in items:
            it.tags = [*it.tags, *extra]
    store = _store()
    added, skipped = store.import_many(items, rule_tagger=tagger(store.rules()), vault_cred_id=cred)
    folders = len({it.folder for it in items if it.folder})
    if json_output:
        _emit_json({"source": src, "path": str(path), "found": len(items), "added": added, "skipped": skipped,
                    "folders": folders})
        return
    if not items:
        ch.warning(f"No bookmarks found in {path.name} (read as {src}).")
        return
    ch.success(f"Imported {added} bookmark(s) from {src} · {skipped} already saved · {folders} folder(s)")
    ch.dim(f"next: {CMD} organize   (AI folder plan, dry run)   ·   {CMD} check   (dead links)")


@app.command("export")
def export_cmd(
    output: Optional[str] = typer.Argument(None, help="File to write; omit to print."),
    fmt: str = typer.Option("html", "--format", "-F", help="html (any browser imports it) | json"),
    folder: Optional[str] = typer.Option(None, "--folder", "-f", help="Only this folder (and below)."),
    tag: Optional[str] = typer.Option(None, "--tag", "-t", help="Only this tag."),
):
    """Export bookmarks as Netscape HTML (with folders) or JSON."""
    from navig_sdk.host import atomic_write_text  # noqa: PLC0415

    from navig_bookmarks.netscape import export_html  # noqa: PLC0415

    kind = fmt.lower().strip()
    if kind not in ("html", "json"):
        ch.error(f"Unknown format {fmt!r}", "html or json")
        raise typer.Exit(2)
    rows = _store().list_bookmarks(folder=_folder_arg(folder), tag=tag, order="insertion")
    if kind == "html":
        text = export_html([
            {"url": b.url, "title": b.title, "notes": b.notes, "folder": b.folder_path, "tags": b.tag_list,
             "added": _epoch(b.created_at)}
            for b in rows
        ])
    else:
        text = json.dumps(
            {"format": "navig-bookmarks", "version": 1, "exported_at": datetime.now(timezone.utc).isoformat(),
             "bookmarks": [b.to_dict() for b in rows]},
            indent=2, ensure_ascii=False,
        ) + "\n"
    if output is None:
        typer.echo(text, nl=False)
        return
    try:
        atomic_write_text(resolve_user_path(output), text)
    except OSError as exc:
        ch.error(f"Could not write {output}", str(exc))
        raise typer.Exit(1) from exc
    ch.success(f"Exported {len(rows)} bookmark(s) to {output}")


# ── organize ────────────────────────────────────────────────────────────────────


def _backup_dir(store) -> Path:  # noqa: ANN001
    return Path(store.db_path).parent / "bookmarks-backups"


@app.command("organize")
def organize_cmd(
    apply: bool = typer.Option(False, "--apply", help="Write the new folders to your bookmarks."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Only show the plan (the default)."),
    force: bool = typer.Option(False, "--force", help="Apply even a plan the failsafe flags as very large."),
    export: Optional[str] = typer.Option(None, "--export", help="Also write the planned tree as Netscape HTML here."),
    folder: Optional[str] = typer.Option(None, "--folder", "-f", help="Only reorganize this folder (and below)."),
    limit: Optional[int] = typer.Option(None, "--limit", "-n", help="Only the first N bookmarks (try it small)."),
    depth: int = typer.Option(3, "--depth", help="Deepest folder level, 1-3."),
    group_min: int = typer.Option(5, "--group-min", help="Send a site with this many bookmarks as one group (0: never)."),
    allow_partial: bool = typer.Option(False, "--allow-partial", help="Keep bookmarks the AI skipped where they are."),
    show: int = typer.Option(15, "--show", help="How many individual moves to list."),
    provider: Optional[str] = typer.Option(None, "--provider", help="AI provider (e.g. openai, ollama, custom)."),
    model: Optional[str] = typer.Option(None, "--model", help="AI model."),
    undo: Optional[str] = typer.Option(None, "--undo", help="Restore the folders saved by an earlier --apply."),
    json_output: bool = typer.Option(False, "--json", help="Print the plan as JSON."),
):
    """Plan a clean folder tree with your AI. Dry run unless --apply."""
    from navig_sdk.host import atomic_write_text  # noqa: PLC0415

    from navig_bookmarks import organize as org  # noqa: PLC0415
    from navig_bookmarks.netscape import export_html  # noqa: PLC0415

    store = _store()
    if undo:
        _undo_organize(store, resolve_user_path(undo))
        return
    if apply and dry_run:
        ch.error("--apply and --dry-run contradict each other; pick one.")
        raise typer.Exit(2)
    if not 1 <= depth <= org.MAX_DEPTH:
        ch.error(f"--depth must be between 1 and {org.MAX_DEPTH}")
        raise typer.Exit(2)
    bookmarks = store.list_bookmarks(folder=_folder_arg(folder), order="insertion", limit=limit)
    if not bookmarks:
        ch.info("Nothing to organize.")
        return
    existing = [f for f, _n in sorted(store.folders().items(), key=lambda kv: -kv[1]) if f]
    result = _run_ai(
        org.plan_organize, bookmarks, _generator(provider, model), existing_folders=existing, max_depth=depth,
        group_min=group_min, progress=_progress("planning batch"),
    )
    current = {b.id: b.folder_path for b in bookmarks}
    plan = dict(result.plan)
    if allow_partial:
        for bid in result.missing:
            plan[bid] = current[bid] or "Unsorted"
    errors = org.validate_plan(plan, current.keys(), max_depth=depth, invented=result.invented)
    errors += result.errors
    if errors:
        if json_output:
            _emit_json({"ok": False, "errors": errors, "missing": result.missing, "invented": result.invented})
        ch.error("The AI's plan is not safe to use:", "\n".join(errors))
        if result.missing and not allow_partial:
            ch.dim("  re-run to retry, or add --allow-partial to keep skipped bookmarks where they are")
        raise typer.Exit(1)

    fs = org.failsafe(plan, current)
    diff = org.diff_rows(plan, current)
    titles = {b.id: (b.title or b.url) for b in bookmarks}
    moves = [(bid, current[bid], plan[bid]) for bid in current if current[bid] != plan[bid]]

    if json_output:
        _emit_json({
            "ok": True, "applied": False, "plan": plan,
            "moves": [{"id": i, "title": titles[i], "from": a, "to": b} for i, a, b in moves],
            "failsafe": {"moves": fs.moves, "total": fs.total, "new_folders": fs.new_folders,
                         "emptied_folders": fs.emptied_folders, "changes": fs.changes, "tripped": fs.tripped,
                         "reasons": fs.reasons},
            "clamped": result.clamped, "batches": result.batches, "calls": result.calls,
        })
    else:
        table = _table(
            ("Folder", {"style": "magenta", "overflow": "fold"}), ("Before", {"justify": "right", "no_wrap": True}),
            ("After", {"justify": "right", "no_wrap": True}), ("Δ", {"justify": "right", "no_wrap": True}),
        )
        for f, before, after in diff[:40]:
            delta = after - before
            color = "green" if delta > 0 else "red"
            table.add_row(_folder_label(f), str(before), str(after), f"[{color}]{delta:+d}[/{color}]")
        _print(table)
        if len(diff) > 40:
            ch.dim(f"  … and {len(diff) - 40} more folder(s)")
        if moves and show > 0:
            mt = _table(("ID", {"style": "cyan", "no_wrap": True}), ("Title", {"overflow": "fold"}),
                        ("From", {"style": "dim", "no_wrap": True, "max_width": 30}),
                        ("To", {"style": "magenta", "no_wrap": True, "max_width": 36}))
            for bid, a, b in moves[:show]:
                mt.add_row(bid, titles[bid], _folder_label(a), _folder_label(b))
            _print(mt)
        ch.dim(
            f"{len(bookmarks)} bookmarks · {fs.moves} move(s) · {fs.new_folders} new folder(s) · "
            f"{fs.emptied_folders} emptied · {result.calls} AI call(s)"
            + (f" · {result.clamped} path(s) cut to {depth} levels" if result.clamped else "")
        )

    if export:
        planned = [
            {"url": b.url, "title": b.title, "notes": b.notes, "folder": plan.get(b.id, b.folder_path),
             "tags": b.tag_list, "added": _epoch(b.created_at)}
            for b in bookmarks
        ]
        try:
            atomic_write_text(resolve_user_path(export), export_html(planned))
        except OSError as exc:
            ch.error(f"Could not write {export}", str(exc))
            raise typer.Exit(1) from exc
        ch.success(f"Planned tree written to {export}")

    if not apply:
        hint = f"Dry run — nothing changed. Apply with {CMD} organize --apply"
        if fs.tripped:
            hint += " --force  (failsafe: " + "; ".join(fs.reasons) + ")"
        if not json_output:
            ch.dim(hint)
        return
    if fs.tripped and not force:
        ch.error("Refusing to apply a plan this large without --force.", "; ".join(fs.reasons))
        raise typer.Exit(1)
    if not moves:
        ch.info("The plan matches the current folders — nothing to apply.")
        return
    from navig_sdk.store import utcnow  # noqa: PLC0415

    backup = _backup_dir(store) / f"organize-{utcnow()[:19].replace(':', '').replace('-', '')}.json"
    try:
        atomic_write_text(backup, json.dumps({"format": "navig-bookmarks-folders", "folders": current}, indent=1))
    except OSError as exc:
        ch.error("Could not write the undo file, so nothing was changed.", str(exc))
        raise typer.Exit(1) from exc
    moved = store.apply_folders(plan)
    ch.success(f"Moved {moved} bookmark(s).")
    ch.dim(f"  undo with: {CMD} organize --undo \"{backup}\"")


def _undo_organize(store, path: Path) -> None:  # noqa: ANN001
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        folders = data["folders"]
        if not isinstance(folders, dict):
            raise ValueError("'folders' is not a mapping")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        ch.error(f"Not an organize undo file: {path}", str(exc))
        raise typer.Exit(2) from exc
    known = {b.id for b in store.list_bookmarks()}
    plan = {str(k): str(v) for k, v in folders.items() if str(k) in known}
    moved = store.apply_folders(plan)
    ch.success(f"Restored {moved} bookmark(s) to their earlier folders.")


# ── check / dedupe ──────────────────────────────────────────────────────────────

_VERDICT_STYLE = {
    "ok": "[green]● ok[/green]", "redirect": "[cyan]↪ redirect[/cyan]", "restricted": "[yellow]◐ restricted[/yellow]",
    "dead": "[red]✗ dead[/red]", "error": "[yellow]⚠ error[/yellow]", "blocked": "[red]⊘ blocked[/red]",
    "skipped": "[dim]○ skipped[/dim]",
}


@app.command("check")
def check_cmd(
    folder: Optional[str] = typer.Option(None, "--folder", "-f", help="Only this folder (and below)."),
    tag: Optional[str] = typer.Option(None, "--tag", "-t", help="Only this tag."),
    limit: Optional[int] = typer.Option(None, "--limit", "-n", help="Only the first N bookmarks."),
    concurrency: int = typer.Option(8, "--concurrency", help="Checks at once (at most 2 per site)."),
    timeout: float = typer.Option(10.0, "--timeout", help="Seconds per request."),
    wayback: bool = typer.Option(True, "--wayback/--no-wayback", help="Look up an archived copy of dead links."),
    show_all: bool = typer.Option(False, "--all", help="List healthy links too."),
    fail_on_dead: bool = typer.Option(False, "--fail-on-dead", help="Exit 1 when a dead link is found."),
    json_output: bool = typer.Option(False, "--json", help="Print as JSON."),
):
    """Find dead links and redirects (headers only; private addresses are never contacted)."""
    from navig_bookmarks.checker import check_many  # noqa: PLC0415

    store = _store()
    rows = store.list_bookmarks(folder=_folder_arg(folder), tag=tag, order="insertion", limit=limit)
    if not rows:
        ch.info("Nothing to check.")
        return
    done = [0]

    def _tick(_res) -> None:  # noqa: ANN001
        done[0] += 1
        if sys.stderr.isatty() and (done[0] % 25 == 0 or done[0] == len(rows)):
            typer.echo(f"  checked {done[0]}/{len(rows)}", err=True)

    results = check_many([(b.id, b.url) for b in rows], concurrency=max(1, concurrency), timeout=timeout,
                         wayback=wayback, on_result=_tick)
    store.set_check_result([(r.id, r.status, r.final_url, r.error) for r in results if r.verdict != "skipped"])
    counts: dict[str, int] = {}
    for r in results:
        counts[r.verdict] = counts.get(r.verdict, 0) + 1
    if json_output:
        _emit_json({"counts": counts, "results": [r.__dict__ for r in results]})
    else:
        titles = {b.id: (b.title or b.url) for b in rows}
        table = _table(
            ("ID", {"style": "cyan", "no_wrap": True}), ("Result", {"no_wrap": True}),
            ("Code", {"justify": "right", "no_wrap": True}), ("Title", {"overflow": "fold"}),
            ("Detail", {"style": "dim", "overflow": "fold"}),
        )
        shown = 0
        for r in results:
            if not show_all and r.verdict == "ok":
                continue
            detail = r.final_url or r.error or ""
            if r.wayback:
                detail = f"archived: {r.wayback}"
            table.add_row(r.id, _VERDICT_STYLE.get(r.verdict, r.verdict), str(r.status or "—"), titles[r.id], detail)
            shown += 1
        if shown:
            _print(table)
        summary = " · ".join(f"{n} {v}" for v, n in sorted(counts.items(), key=lambda kv: -kv[1]))
        ch.dim(f"{len(results)} checked · {summary}")
    if fail_on_dead and counts.get("dead"):
        raise typer.Exit(1)


def _keep_choice(group: list[Bookmark]) -> Bookmark:
    """Keep the one with a credential, then the most visited, then the oldest."""
    return sorted(group, key=lambda b: (b.vault_cred_id is None, -b.visit_count, b.created_at or ""))[0]


@app.command("dedupe")
def dedupe_cmd(
    apply: bool = typer.Option(False, "--apply", help="Merge the duplicates (tags, notes and visits are kept)."),
    json_output: bool = typer.Option(False, "--json", help="Print as JSON."),
):
    """Find bookmarks that point at the same page (tracking parameters, www., http/https ignored)."""
    from navig_bookmarks.urls import dedupe_key  # noqa: PLC0415

    store = _store()
    groups: dict[str, list[Bookmark]] = {}
    for b in store.list_bookmarks(order="insertion"):
        groups.setdefault(dedupe_key(b.url), []).append(b)
    dupes = {k: g for k, g in groups.items() if len(g) > 1}
    plan = []
    for group in dupes.values():
        keep = _keep_choice(group)
        plan.append((keep, [b for b in group if b.id != keep.id]))
    if json_output and not apply:
        _emit_json([{"keep": k.id, "url": k.url, "merge": [d.id for d in drop]} for k, drop in plan])
        return
    if not plan:
        ch.success("No duplicates.")
        return
    if not json_output:
        table = _table(("Keep", {"style": "green", "no_wrap": True}), ("Merge", {"style": "red", "no_wrap": True}),
                       ("URL", {"overflow": "fold"}))
        for keep, drop in plan[:80]:
            table.add_row(keep.id, ", ".join(d.id for d in drop), keep.url)
        _print(table)
    extra = sum(len(drop) for _k, drop in plan)
    if not apply:
        ch.dim(f"{len(plan)} page(s) saved more than once · {extra} extra · merge with {CMD} dedupe --apply")
        return
    merged = sum(store.merge(keep.id, [d.id for d in drop]) for keep, drop in plan)
    if json_output:
        _emit_json({"merged": merged, "groups": len(plan)})
        return
    ch.success(f"Merged {merged} duplicate(s) into {len(plan)} bookmark(s).")


# ── rules ───────────────────────────────────────────────────────────────────────


@rules_app.command("list")
def rules_list(json_output: bool = typer.Option(False, "--json", help="Print as JSON.")):
    """List tagging rules."""
    rules = _store().rules()
    if json_output:
        _emit_json([{"id": rid, "pattern": p, "tags": t} for rid, p, t in rules])
        return
    if not rules:
        ch.info(f"No rules yet · e.g. {CMD} rules add github.com dev")
        return
    table = _table(("#", {"style": "cyan", "no_wrap": True}), ("Pattern", {"no_wrap": True}), ("Tags", {"style": "yellow"}))
    for rid, pattern, tags in rules:
        table.add_row(str(rid), pattern, ", ".join(tags))
    _print(table)


@rules_app.command("add")
def rules_add(
    pattern: str = typer.Argument(..., help="example.com, example.com/path, example.com/search?q, …"),
    tags: list[str] = typer.Argument(..., help="Tags to give matching bookmarks."),
):
    """Add a rule (applies to new bookmarks; run `rules apply` for existing ones)."""
    if "." not in pattern.split("/")[0]:
        ch.error(f"{pattern!r} has no domain", "a rule starts with a domain, e.g. github.com or docs.python.org/3")
        raise typer.Exit(2)
    rid = _store().add_rule(pattern, tags)
    ch.success(f"Rule {rid}: {pattern} -> {', '.join(tags)}")


@rules_app.command("remove")
def rules_remove(rule_id: int = typer.Argument(..., help="Rule number from `rules list`.")):
    """Remove a rule (its tags stay until `rules apply`)."""
    if not _store().remove_rule(rule_id):
        ch.error(f"No rule {rule_id}")
        raise typer.Exit(2)
    ch.success(f"Removed rule {rule_id}")


@rules_app.command("apply")
def rules_apply(dry_run: bool = typer.Option(False, "--dry-run", help="Show what would change.")):
    """Re-tag every bookmark from the current rules (only rule tags are replaced)."""
    from navig_bookmarks.rules import tags_for  # noqa: PLC0415

    store = _store()
    rules = store.rules()
    changed = 0
    for b in store.list_bookmarks(order="insertion"):
        wanted = tags_for(b.url, rules)
        held_by_others = {t for t, src in b.tags.items() if src != "rule"}
        have = {t for t, src in b.tags.items() if src == "rule"}
        if set(wanted) - held_by_others == have:
            continue
        if not dry_run:
            store.replace_source_tags(b.id, wanted, "rule")
        changed += 1
    verb = "would change" if dry_run else "changed"
    ch.success(f"Rules {verb} the tags of {changed} bookmark(s).")


# ── housekeeping ────────────────────────────────────────────────────────────────


@app.command("folders")
def folders_cmd(json_output: bool = typer.Option(False, "--json", help="Print as JSON.")):
    """Folders and how many bookmarks each holds."""
    folders = _store().folders()
    if json_output:
        _emit_json(folders)
        return
    table = _table(("Folder", {"style": "magenta", "overflow": "fold"}), ("Bookmarks", {"justify": "right"}))
    for f, n in folders.items():
        table.add_row(_folder_label(f) if f else "[dim](no folder)[/dim]", str(n))
    _print(table)


@app.command("tags")
def tags_cmd(json_output: bool = typer.Option(False, "--json", help="Print as JSON.")):
    """Tags and how often each is used."""
    counts = _store().tag_counts()
    if json_output:
        _emit_json(counts)
        return
    if not counts:
        ch.info(f"No tags yet · {CMD} tag --ai --untagged")
        return
    table = _table(("Tag", {"style": "yellow", "no_wrap": True}), ("Bookmarks", {"justify": "right"}))
    for t, n in counts.items():
        table.add_row(t, str(n))
    _print(table)


@app.command("migrate")
def migrate_cmd(
    source: Optional[str] = typer.Option(None, "--from", help="A links.db to copy (default: navig's own)."),
):
    """Copy bookmarks from navig's old `links` store (also done automatically; safe to repeat)."""
    from navig_bookmarks.migrate import migrate_legacy_links  # noqa: PLC0415

    path = resolve_user_path(source) if source else None
    if path is not None and not path.exists():
        ch.error(f"Not found: {source}")
        raise typer.Exit(2)
    try:
        res = migrate_legacy_links(_store(), path)
    except (sqlite3.DatabaseError, OSError) as exc:
        ch.error("Could not read the links store.", str(exc))
        raise typer.Exit(1) from exc
    if res.status == "absent":
        ch.info(f"No links store at {res.source} — nothing to copy.")
    elif res.status == "unchanged":
        ch.info("Already copied; the links store has not changed since.")
    else:
        ch.success(f"Copied {res.copied} bookmark(s) · {res.skipped_duplicates} already here · "
                   f"{res.already_migrated} copied before")


@app.command("version")
def version_cmd():
    """Print the plugin version."""
    typer.echo(f"navig-bookmarks {__version__}")
