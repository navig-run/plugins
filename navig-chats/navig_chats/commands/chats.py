"""``navig chats`` / ``navig-chats`` — the chat library's command surface.

The same store the Dock's ChatGPT Lens and the library page read through
``/api/deck/chats/*``. Exit codes follow navig's plugin contract: 2 for usage (unknown id, bad
argument), 1 for an operation that failed, 0 otherwise.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Optional

import typer

from navig_sdk import console as ch
from navig_sdk.host import command_name

from navig_chats import __version__
from navig_chats._typed_path import resolve_user_path

CMD = command_name("chats")

app = typer.Typer(
    name="chats",
    help="💬  Your AI chat library: import ChatGPT exports, search every message, folders, export.",
    no_args_is_help=True,
)
folders_app = typer.Typer(name="folders", help="NAVIG folders (a local overlay — not ChatGPT Projects).", no_args_is_help=True)
app.add_typer(folders_app, name="folders")

_STORES: dict[str, object] = {}


def _store():
    from navig_chats.store import ChatStore, default_db_path  # noqa: PLC0415

    path = default_db_path()
    key = str(path)
    if key not in _STORES:
        try:
            _STORES[key] = ChatStore(path)
        except sqlite3.DatabaseError as exc:
            ch.error(f"Cannot open the chat library at {path}", str(exc))
            raise typer.Exit(1) from exc
    return _STORES[key]


def _get(store, ref: str) -> dict:
    conv = store.resolve(ref)
    if conv is None:
        ch.error(f"No chat {ref!r} in the library", f"find one with: {CMD} search <words>")
        raise typer.Exit(2)
    return conv


def _table(*columns: tuple[str, dict]):
    from rich.table import Table  # noqa: PLC0415

    table = Table(box=None, show_header=True, padding=(0, 2), header_style="bold")
    for name, kw in columns:
        table.add_column(name, **kw)
    return table


def _emit_json(data) -> None:  # noqa: ANN001
    typer.echo(json.dumps(data, indent=2, ensure_ascii=False, default=str))


_DOT = {
    "complete": "[green]●[/green]",
    "rendered": "[cyan]●[/cyan]",
    "partial": "[yellow]◐[/yellow]",
    "url_only": "[dim]○[/dim]",
}


def _conv_rows(rows: list[dict]):
    table = _table(
        ("", {"no_wrap": True}),
        ("Title", {"overflow": "fold"}),
        ("Msgs", {"justify": "right", "no_wrap": True}),
        ("Updated", {"style": "dim", "no_wrap": True}),
        ("ID", {"style": "cyan", "no_wrap": True}),
    )
    for r in rows:
        title = r["display_title"] + (" [magenta]📌[/magenta]" if r["pinned"] else "")
        table.add_row(_DOT.get(r["completeness"], "?"), title, str(r["message_count"]),
                      (r["updated_at"] or r["created_at"] or "—")[:10], r["native_id"][:13])
    return table


def _snippet(text: str) -> str:
    from rich.markup import escape  # noqa: PLC0415

    from navig_chats.store import MARK_END, MARK_START  # noqa: PLC0415

    out = escape(re.sub(r"\s+", " ", text or "").strip())
    return out.replace(MARK_START, "[bold yellow]").replace(MARK_END, "[/bold yellow]")


@app.callback(invoke_without_command=True)
def _main(version: bool = typer.Option(False, "--version", help="Print the version and exit.")) -> None:
    if version:
        typer.echo(__version__)
        raise typer.Exit(0)


# ── import ───────────────────────────────────────────────────────────────────


@app.command("import")
def import_cmd(
    path: Path = typer.Argument(..., help="Your ChatGPT data export: the .zip, its folder, or conversations.json."),
    json_output: bool = typer.Option(False, "--json", help="Print the result as JSON."),
):
    """Import a ChatGPT account export. Safe to re-run: unchanged chats are skipped."""
    from navig_chats.chatgpt_export import ExportFormatError, import_export  # noqa: PLC0415

    source = resolve_user_path(path)
    if not source.exists():
        ch.error(f"No such file: {source}")
        raise typer.Exit(2)

    def progress(p: dict) -> None:
        if not json_output and sys.stderr.isatty():
            typer.echo(f"  {p['done']} chats · {p['added']} new · {p['updated']} updated", err=True)

    try:
        res = import_export(_store(), source, progress=progress)
    except ExportFormatError as exc:
        ch.error("That is not a ChatGPT export I can read.", str(exc))
        raise typer.Exit(2) from exc
    if json_output:
        _emit_json(res)
    else:
        mark = ch.success if res["status"] == "done" else ch.warning
        mark(f"Imported {res['done']} chat(s): {res['added']} new, {res['updated']} updated, {res['unchanged']} unchanged")
        for e in res["errors"][:10]:
            ch.dim(f"  skipped {e.get('title') or e.get('index')}: {e['error']}")
        if len(res["errors"]) > 10:
            ch.dim(f"  … and {len(res['errors']) - 10} more")
        ch.dim(f"next: {CMD} search <words>  ·  {CMD} list")
    if res["status"] == "failed":
        raise typer.Exit(1)


@app.command("restore")
def restore_cmd(
    path: Path = typer.Argument(..., help="A NAVIG chat archive (.zip) or one .json bundle."),
    only: Optional[str] = typer.Option(None, "--only", help="Comma-separated chat ids to restore."),
):
    """Restore chats from a NAVIG export into the library (not into ChatGPT itself)."""
    from navig_chats.export import restore_bundle  # noqa: PLC0415

    source = resolve_user_path(path)
    if not source.exists():
        ch.error(f"No such file: {source}")
        raise typer.Exit(2)
    try:
        res = restore_bundle(_store(), source, only={x.strip() for x in only.split(",")} if only else None)
    except (ValueError, KeyError) as exc:
        ch.error("That is not a NAVIG chat archive.", str(exc))
        raise typer.Exit(2) from exc
    ch.success(f"Restored: {res['added']} new, {res['updated']} updated, {res['unchanged']} unchanged")


# ── list / search / show ─────────────────────────────────────────────────────


@app.command("list")
def list_cmd(
    folder: Optional[str] = typer.Option(None, "--folder", "-f", help="Only this folder id (and below)."),
    tag: Optional[str] = typer.Option(None, "--tag", "-t", help="Only this tag."),
    collection: str = typer.Option("all", "--collection", "-c", help="all · recent · uncategorized · partial · pinned · favorites · archived · projects"),
    sort: str = typer.Option("newest", "--sort", "-s", help="newest · oldest · created · alpha · visited · manual"),
    limit: int = typer.Option(50, "--limit", "-n", help="At most this many."),
    json_output: bool = typer.Option(False, "--json", help="Print as JSON."),
):
    """List chats in the library."""
    rows, total = _store().list(folder=folder, tag=tag, collection=collection, sort=sort, limit=limit)
    if json_output:
        _emit_json({"items": rows, "total": total})
        return
    if not rows:
        ch.info(f"No chats here yet · import your ChatGPT export with {CMD} import <zip>")
        return
    ch.console.print(_conv_rows(rows))
    ch.dim(f"{len(rows)} of {total} · ● complete  ● rendered  ◐ partial  ○ link only · read one with {CMD} show <id>")


@app.command("search")
def search_cmd(
    query: str = typer.Argument(..., help='Words, "exact phrases", -excluded, prefix*, a OR b.'),
    role: Optional[str] = typer.Option(None, "--role", help="Only messages by: user · assistant · tool"),
    code: bool = typer.Option(False, "--code", help="Only messages with code."),
    folder: Optional[str] = typer.Option(None, "--folder", "-f", help="Only this folder id (and below)."),
    limit: int = typer.Option(20, "--limit", "-n", help="At most this many chats."),
    json_output: bool = typer.Option(False, "--json", help="Print as JSON."),
):
    """Search every message and title, ranked by relevance."""
    from navig_chats.query import EmptyQuery  # noqa: PLC0415

    try:
        res = _store().search(query, role=role, code_only=code, folder=folder, limit=limit)
    except EmptyQuery:
        ch.error("Nothing to search for", "add at least one word that is not excluded")
        raise typer.Exit(2) from None
    if json_output:
        _emit_json(res)
        return
    if not res["results"]:
        ch.info(f"Nothing matches {query!r}")
        return
    table = _table(("Chat", {"overflow": "fold", "ratio": 2}), ("Best match", {"overflow": "fold", "ratio": 3}),
                   ("Hits", {"justify": "right", "no_wrap": True}), ("ID", {"style": "cyan", "no_wrap": True}))
    for r in res["results"]:
        hit = r["hits"][0]["snippet"] if r["hits"] else "[dim](title)[/dim]"
        table.add_row(r["display_title"], _snippet(hit), str(r["match_count"]), r["native_id"][:13])
    ch.console.print(table)
    ch.dim(f"{len(res['results'])} chat(s) · read one with {CMD} show <id>")


@app.command("show")
def show_cmd(
    ref: str = typer.Argument(..., help="Chat id (a unique part is enough)."),
    all_branches: bool = typer.Option(False, "--all-branches", help="Include regenerated / edited branches."),
    json_output: bool = typer.Option(False, "--json", help="Print as JSON."),
):
    """Read one chat."""
    store = _store()
    conv = _get(store, ref)
    msgs = store.messages(conv["id"], branch="all" if all_branches else "active")
    if json_output:
        _emit_json({"conversation": conv, "messages": msgs})
        return
    from rich.markup import escape  # noqa: PLC0415

    from navig_chats.export import role_label  # noqa: PLC0415

    ch.console.print(f"[bold]{escape(conv['display_title'])}[/bold]  {_DOT.get(conv['completeness'], '')} {conv['completeness']}")
    ch.dim(f"{conv['url']}  ·  {len(msgs)} message(s)")
    for m in msgs:
        ch.console.print(f"\n[bold cyan]{role_label(m['role'], conv['provider'])}[/bold cyan]")
        ch.console.print(escape(m["text"]), soft_wrap=True)


@app.command("export")
def export_cmd(
    refs: Optional[list[str]] = typer.Argument(None, help="Chat ids. Leave out with --folder or --all."),
    out: Path = typer.Option(..., "--out", "-o", help="File to write (.md/.txt/.html/.json, or .zip for many)."),
    folder: Optional[str] = typer.Option(None, "--folder", "-f", help="Every chat in this folder (and below)."),
    everything: bool = typer.Option(False, "--all", help="The whole library."),
):
    """Export chats — readable without NAVIG. Several chats go into one .zip with a manifest."""
    from navig_chats import export as ex  # noqa: PLC0415

    store = _store()
    dest = resolve_user_path(out)
    fmt = dest.suffix.lstrip(".").lower()
    if refs:
        ids = [_get(store, r)["id"] for r in refs]
    elif folder or everything:
        ids, offset = [], 0
        while True:
            rows, total = store.list(folder=folder, collection="all", sort="oldest", limit=1000, offset=offset)
            ids += [r["id"] for r in rows]
            offset += len(rows)
            if not rows or offset >= total:
                break
    else:
        ch.error("Say what to export", f"chat ids, --folder <id> or --all · e.g. {CMD} export --all -o chats.zip")
        raise typer.Exit(2)
    if not ids:
        ch.info("Nothing to export")
        return
    if fmt == "zip":
        manifest = ex.write_zip(store, ids, dest)
        ch.success(f"Wrote {manifest['count']} chat(s) to {dest}")
        if manifest["skipped_attachments"]:
            ch.dim(f"{len(manifest['skipped_attachments'])} attachment(s) were never in the library — listed in manifest.json")
        return
    if fmt not in ex.FORMATS:
        ch.error(f"Unknown format .{fmt}", "use .md, .txt, .html, .json or .zip")
        raise typer.Exit(2)
    if len(ids) > 1:
        ch.error("Several chats go into a .zip", f"e.g. {CMD} export … -o chats.zip")
        raise typer.Exit(2)
    _name, content = ex.export_one(store, ids[0], fmt)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    ch.success(f"Wrote {dest}")


@app.command("stats")
def stats_cmd(json_output: bool = typer.Option(False, "--json", help="Print as JSON.")):
    """How much the library holds, and where."""
    store = _store()
    s = store.stats()
    s["index_intact"] = store.integrity()
    if json_output:
        _emit_json(s)
        return
    table = _table(("Item", {"style": "dim", "no_wrap": True}), ("Value", {"overflow": "fold"}))
    for k in ("conversations", "messages", "complete", "rendered", "partial", "folders"):
        table.add_row(k.capitalize(), str(s[k]))
    table.add_row("Search index", "[green]● intact[/green]" if s["index_intact"] else f"[red]✗ damaged — run {CMD} reindex[/red]")
    table.add_row("Database", s["db_path"])
    ch.console.print(table)


@app.command("reindex")
def reindex_cmd():
    """Rebuild the search index from the stored messages."""
    store = _store()
    store.rebuild_index()
    if store.integrity():
        ch.success("Search index rebuilt")
    else:
        ch.error("The index is still damaged after a rebuild", "the database itself may be corrupt")
        raise typer.Exit(1)


# ── folders ──────────────────────────────────────────────────────────────────


@folders_app.command("list")
def folders_list(json_output: bool = typer.Option(False, "--json", help="Print as JSON.")):
    """Every folder, as a tree."""
    folders = _store().folders()
    if json_output:
        _emit_json(folders)
        return
    if not folders:
        ch.info(f"No folders yet · create one with {CMD} folders add <name>")
        return
    by_parent: dict = {}
    for f in folders:
        by_parent.setdefault(f["parent_id"], []).append(f)
    table = _table(("Folder", {"overflow": "fold"}), ("Chats", {"justify": "right", "no_wrap": True}), ("ID", {"style": "cyan", "no_wrap": True}))

    def walk(parent, depth):
        for f in by_parent.get(parent, []):
            table.add_row("  " * depth + ("📌 " if f["pinned"] else "") + f["name"], str(f["count"]), f["id"])
            walk(f["id"], depth + 1)

    walk(None, 0)
    ch.console.print(table)


@folders_app.command("add")
def folders_add(
    name: str = typer.Argument(..., help="Folder name."),
    parent: Optional[str] = typer.Option(None, "--parent", "-p", help="Parent folder id."),
):
    """Create a folder."""
    from navig_chats.store import NotFound  # noqa: PLC0415

    try:
        f = _store().create_folder(name, parent_id=parent)
    except (NotFound, ValueError) as exc:
        ch.error("Could not create the folder", str(exc))
        raise typer.Exit(2) from exc
    ch.success(f"Created {f['name']} ({f['id']})")


@folders_app.command("rm")
def folders_rm(fid: str = typer.Argument(..., help="Folder id.")):
    """Delete a folder. Its chats stay in the library; subfolders move up a level."""
    if not _store().delete_folder(fid):
        ch.error(f"No folder {fid!r}")
        raise typer.Exit(2)
    ch.success("Folder deleted (its chats are kept)")


@app.command("move")
def move_cmd(
    refs: list[str] = typer.Argument(..., help="Chat ids."),
    folder: str = typer.Option(..., "--to", help="Folder id, or 'none' to unfile."),
    keep: bool = typer.Option(False, "--keep", help="Add to the folder and keep the chat's other folders."),
):
    """Put chats in a NAVIG folder (a local change — nothing moves in ChatGPT)."""
    from navig_chats.store import NotFound  # noqa: PLC0415

    store = _store()
    ids = [_get(store, r)["id"] for r in refs]
    try:
        if folder == "none":
            store.bulk(ids, "move", None)
        else:
            store.bulk(ids, "add_to" if keep else "move", folder)
    except NotFound as exc:
        ch.error("No such folder", str(exc))
        raise typer.Exit(2) from exc
    ch.success(f"Moved {len(ids)} chat(s)")


# ── sharing with NAVIG's knowledge index ─────────────────────────────────────

_LEVEL_HELP = "off · local (navig recall on this computer only) · agent (may reach your AI provider in agent context) · inherit"


@app.command("share")
def share_cmd(
    refs: Optional[list[str]] = typer.Argument(None, help="Chat ids. Leave out with --folder."),
    level: str = typer.Option(..., "--level", "-l", help=_LEVEL_HELP),
    folder: Optional[str] = typer.Option(None, "--folder", "-f", help="Set it on a folder (its subfolders inherit)."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="With --folder: the NAVIG project these chats belong to."),
):
    """Choose what NAVIG's knowledge index may read. Nothing is shared until you say so."""
    from navig_chats.store import BRAIN_LEVELS, NotFound  # noqa: PLC0415

    value = None if level == "inherit" else level
    if value is not None and value not in BRAIN_LEVELS:
        ch.error(f"Unknown level {level!r}", _LEVEL_HELP)
        raise typer.Exit(2)
    store = _store()
    try:
        if folder:
            fields: dict = {"brain": value}
            if project is not None:
                fields["brain_project"] = project
            f = store.update_folder(folder, **fields)
            ch.success(f"Folder {f['name']}: {level}" + (f" · project {f['brain_project']}" if f.get("brain_project") else ""))
        elif refs:
            for r in refs:
                conv = store.update(_get(store, r)["id"], brain=value)
                ch.success(f"{conv['display_title']}: {level}")
        else:
            ch.error("Say what to share", f"chat ids, or --folder <id> · e.g. {CMD} share 67f1c2d3 --level local")
            raise typer.Exit(2)
    except (NotFound, ValueError) as exc:
        ch.error("Could not change sharing", str(exc))
        raise typer.Exit(2) from exc
    if value == "agent":
        ch.warning("At 'agent', these chats can be included in agent context sent to your configured AI provider.")
    ch.dim("index them: navig recall ingest --source chats")


@app.command("shared")
def shared_cmd(json_output: bool = typer.Option(False, "--json", help="Print as JSON.")):
    """Every chat NAVIG's knowledge index may read, and at which level."""
    from navig_chats.store import brain_shared  # noqa: PLC0415

    rows = brain_shared(_store())
    if json_output:
        _emit_json([{"id": r["id"], "title": r["display_title"], "level": r["brain_level"], "project": r["brain_project"]} for r in rows])
        return
    if not rows:
        ch.info(f"Nothing is shared · share a folder with {CMD} share --folder <id> --level local")
        return
    table = _table(("Level", {"no_wrap": True}), ("Chat", {"overflow": "fold"}), ("Project", {"style": "dim", "no_wrap": True}),
                   ("ID", {"style": "cyan", "no_wrap": True}))
    for r in rows:
        lvl = "[yellow]● agent[/yellow]" if r["brain_level"] == "agent" else "[green]● local[/green]"
        table.add_row(lvl, r["display_title"], r["brain_project"] or "chats", r["native_id"][:13])
    ch.console.print(table)
    ch.dim("local = this computer only · agent = may reach your AI provider in agent context")


# ── continue this work ───────────────────────────────────────────────────────


@app.command("handoff")
def handoff_cmd(
    refs: Optional[list[str]] = typer.Argument(None, help="Chat ids. Leave out with --folder."),
    folder: Optional[str] = typer.Option(None, "--folder", "-f", help="Every chat in this folder (and below)."),
    goal: Optional[str] = typer.Option(None, "--goal", "-g", help="What the next agent should do with it."),
    title: Optional[str] = typer.Option(None, "--title", help="Pack title."),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Write the Markdown here."),
    into: Optional[Path] = typer.Option(None, "--into", help="A space/project folder: write to its .navig/plans/briefs/."),
    json_output: bool = typer.Option(False, "--json", help="The pack as JSON (items, sources, missing)."),
):
    """Continue this work — a context pack (decisions, open items, code, sources) for another agent."""
    from navig_chats import handoff as ho  # noqa: PLC0415

    store = _store()
    if refs:
        ids = [_get(store, r)["id"] for r in refs]
    elif folder:
        ids, offset = [], 0
        while True:
            rows, total = store.list(folder=folder, collection="all", sort="oldest", limit=1000, offset=offset)
            ids += [r["id"] for r in rows]
            offset += len(rows)
            if not rows or offset >= total:
                break
    else:
        ch.error("Say what to hand off", f"chat ids or --folder <id> · e.g. {CMD} handoff 67f1c2d3 -o next.md")
        raise typer.Exit(2)
    try:
        pack = ho.build_handoff(store, ids, title=title, goal=goal)
    except LookupError as exc:
        ch.error("Nothing to hand off", str(exc))
        raise typer.Exit(2) from exc
    if json_output:
        typer.echo(ho.pack_json(pack))
        return
    if into:
        try:
            target = ho.write_into_space(resolve_user_path(into), pack)
        except FileExistsError as exc:
            ch.error(str(exc))
            raise typer.Exit(1) from exc
        ch.success(f"Wrote {target}")
    if out:
        dest = resolve_user_path(out)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(pack["markdown"], encoding="utf-8")
        ch.success(f"Wrote {dest}")
    if not out and not into:
        typer.echo(pack["markdown"])
        return
    c = pack["counts"]
    ch.dim(f"{c.get('decision:current', 0)} decision(s) · {c.get('task:open', 0) + c.get('question:open', 0)} open item(s) · "
           f"{len(pack['missing'])} gap(s) listed under 'Missing or incomplete context'")
