"""``navig notes`` / ``navig-notes`` — your web sticky notes, from the command line.

The same store the NAVIG Dock syncs through ``/api/deck/notes/*``. A note added here appears
in the browser on the Dock's next sync (a few seconds after any edit, every 5 minutes, and when
Chrome starts). Exit codes follow navig's plugin contract: 2 for usage (unknown id, bad
argument), 1 for an operation that failed, 0 otherwise.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer

from navig_sdk import console as ch
from navig_sdk.host import command_name

from navig_notes._typed_path import resolve_user_path

CMD = command_name("notes")

app = typer.Typer(
    name="notes",
    help="📝  Your web sticky notes (from the NAVIG Dock): list, search, add, export.",
    no_args_is_help=True,
)

_STORES: dict[str, object] = {}

_SCOPE = {"page": "[dim]page[/dim]", "site": "[cyan]site[/cyan]", "global": "[yellow]everywhere[/yellow]"}
_STATE = {"open": "[green]●[/green]", "minimized": "[cyan]◐[/cyan]", "hidden": "[dim]○[/dim]"}


def _store():
    from navig_notes.store import NoteStore, default_db_path  # noqa: PLC0415

    path = default_db_path()
    key = str(path)
    if key not in _STORES:
        try:
            _STORES[key] = NoteStore(path)
        except sqlite3.DatabaseError as exc:
            ch.error(f"Cannot open the notes store at {path}", str(exc))
            raise typer.Exit(1) from exc
    return _STORES[key]


def _table(*columns: tuple[str, dict]):
    from rich.table import Table  # noqa: PLC0415

    table = Table(box=None, show_header=True, padding=(0, 2), header_style="bold")
    for name, kw in columns:
        table.add_column(name, **kw)
    return table


def _emit_json(data) -> None:  # noqa: ANN001
    typer.echo(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def _when(ms) -> str:  # noqa: ANN001
    if not ms:
        return "—"
    return datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M")


def _where(n: dict) -> str:
    if n.get("scope") == "global":
        return "every site"
    if n.get("scope") == "site":
        return n.get("siteKey") or "—"
    return n.get("pageKey") or n.get("sourceUrl") or "—"


def _rows(notes: list[dict], *, snippet: bool = False):
    from rich.markup import escape  # noqa: PLC0415

    from navig_notes.store import display_title  # noqa: PLC0415

    t = _table(
        ("", {"no_wrap": True, "width": 1}),
        ("Id", {"no_wrap": True, "style": "dim"}),
        ("Note", {"overflow": "fold", "ratio": 3}),
        ("Shows on", {"no_wrap": True, "max_width": 42, "overflow": "ellipsis"}),
        ("Scope", {"no_wrap": True}),
        ("Edited", {"no_wrap": True, "style": "dim"}),
    )
    for n in notes:
        text = f"[bold]{escape(display_title(n))}[/bold]"
        if snippet and n.get("snippet"):
            snip = escape(n["snippet"].replace("\n", " ")).replace("\x02", "[reverse]").replace("\x03", "[/reverse]")
            text += f"\n[dim]{snip}[/dim]"
        tags = " ".join(f"#{x}" for x in n.get("tags") or [])
        if tags:
            text += f"  [magenta]{escape(tags)}[/magenta]"
        t.add_row(_STATE.get(n.get("state"), "●"), n["id"][:8], text, escape(_where(n)), _SCOPE.get(n.get("scope"), "—"),
                  _when(n.get("updatedAt")))
    return t


def _get(store, ref: str) -> dict:
    note = store.resolve(ref)
    if note is None:
        ch.error(f"No note {ref!r}", f"find one with: {CMD} search <words>  ·  {CMD} list")
        raise typer.Exit(2)
    return note


@app.command("list")
def list_cmd(
    site: Optional[str] = typer.Option(None, "--site", "-s", help="Only notes for this website (e.g. github.com)."),
    scope: Optional[str] = typer.Option(None, "--scope", help="page, site or global (everywhere)."),
    tag: Optional[str] = typer.Option(None, "--tag", "-t", help="Only this tag."),
    limit: int = typer.Option(50, "--limit", "-n", min=1, max=2000, help="How many to show."),
    json_output: bool = typer.Option(False, "--json", help="Print the notes as JSON."),
) -> None:
    """Your notes, most recently edited first."""
    from navig_notes.store import SCOPES  # noqa: PLC0415

    if scope and scope not in SCOPES:
        ch.error(f"--scope must be one of {', '.join(SCOPES)}")
        raise typer.Exit(2)
    store = _store()
    notes = store.list(site=site, scope=scope, tag=tag, limit=limit)
    if json_output:
        _emit_json(notes)
        return
    if not notes:
        ch.info("No notes yet." if not (site or scope or tag) else "No notes match.",
                "make one on any web page with Alt+Shift+N (NAVIG Dock), or: " f"{CMD} add \"text\"")
        return
    ch.console.print(_rows(notes))
    total = store.stats()["notes"]
    ch.dim(f"{len(notes)} of {total} · ● showing  ◐ minimized  ○ hidden · read one with {CMD} show <id>")


@app.command("search")
def search_cmd(
    words: list[str] = typer.Argument(..., help='Words to find (all must match; "quote a phrase").'),
    limit: int = typer.Option(30, "--limit", "-n", min=1, max=500),
    json_output: bool = typer.Option(False, "--json", help="Print the matches as JSON."),
) -> None:
    """Search every note's title, text, tags, page title and website."""
    store = _store()
    q = " ".join(words)
    hits = store.search(q, limit=limit)
    if json_output:
        _emit_json(hits)
        return
    if not hits:
        ch.info(f"Nothing matches {q!r}", f"see everything with: {CMD} list")
        return
    ch.console.print(_rows(hits, snippet=True))
    ch.dim(f"{len(hits)} match{'es' if len(hits) != 1 else ''} · read one with {CMD} show <id>")


@app.command("show")
def show_cmd(
    ref: str = typer.Argument(..., help="A note id (the first characters are enough)."),
    json_output: bool = typer.Option(False, "--json", help="Print the note as JSON."),
) -> None:
    """One note in full."""
    from rich.markup import escape  # noqa: PLC0415

    from navig_notes.store import display_title  # noqa: PLC0415

    note = _get(_store(), ref)
    if json_output:
        _emit_json(note)
        return
    ch.console.print(f"[bold]{escape(display_title(note))}[/bold]  {_SCOPE.get(note['scope'], '')}  {_STATE.get(note.get('state'), '')}")
    ch.dim(f"{note['id']} · shows on {_where(note)} · edited {_when(note.get('updatedAt'))} · from {note.get('origin', 'dock')}")
    if note.get("sourceUrl"):
        ch.dim(f"page: {note['sourceUrl']}")
    if note.get("tags"):
        ch.dim(" ".join(f"#{t}" for t in note["tags"]))
    ch.console.print()
    ch.raw_print(note.get("body") or "(no text)")


@app.command("add")
def add_cmd(
    text: list[str] = typer.Argument(..., help="The note's text (Markdown works)."),
    url: str = typer.Option("", "--url", "-u", help="The web page it belongs to (needed for --scope page or site)."),
    scope: str = typer.Option("", "--scope", help="page, site or global. Default: page with --url, else global."),
    title: str = typer.Option("", "--title", help="A title (default: the first line)."),
    tag: Optional[list[str]] = typer.Option(None, "--tag", "-t", help="A tag (repeatable)."),
    json_output: bool = typer.Option(False, "--json", help="Print the new note as JSON."),
) -> None:
    """Add a note — it appears in the browser on the Dock's next sync."""
    from navig_notes.store import NoteError  # noqa: PLC0415

    scope = scope or ("page" if url else "global")
    try:
        note = _store().create(body=" ".join(text), title=title, url=url, scope=scope, tags=tag or [], origin="cli")
    except NoteError as exc:
        ch.error(str(exc), f"e.g. {CMD} add \"check the retry path\" --url https://github.com/org/repo/issues/12")
        raise typer.Exit(2) from exc
    if json_output:
        _emit_json(note)
        return
    ch.success(f"Note {note['id'][:8]} added — shows on {_where(note)}", "the NAVIG Dock picks it up on its next sync")


@app.command("rm")
def rm_cmd(
    ref: str = typer.Argument(..., help="A note id (the first characters are enough)."),
) -> None:
    """Delete a note everywhere (the Dock moves it to its Trash on the next sync)."""
    store = _store()
    note = _get(store, ref)
    result = store.delete(note["id"])
    if result != "deleted":
        ch.error(f"Note {note['id'][:8]} was not deleted ({result})")
        raise typer.Exit(1)
    ch.success(f"Deleted note {note['id'][:8]}", "undo from the Dock: Notes Library → Trash → Restore")


@app.command("export")
def export_cmd(
    path: Path = typer.Argument(..., help="Where to write: .json (re-importable in the Dock) or .md."),
    site: Optional[str] = typer.Option(None, "--site", "-s", help="Only notes for this website."),
) -> None:
    """Export notes to a file the Dock's Library can import (JSON) or anyone can read (Markdown)."""
    from navig_notes.store import display_title  # noqa: PLC0415

    target = resolve_user_path(path)
    notes = _store().list(site=site, limit=100_000)
    if not notes:
        ch.info("No notes to export.")
        return
    if target.suffix.lower() == ".md":
        lines = ["# NAVIG notes", ""]
        for n in notes:
            lines += [f"## {display_title(n)}", "", f"_{n['scope']} · {_where(n)}_", "", n.get("body") or "", ""]
        text = "\n".join(lines)
    else:
        clean = [{k: v for k, v in n.items() if k not in ("origin", "share")} for n in notes]
        text = json.dumps({"format": "navig-notes", "schema": 2, "exportedAt": datetime.now().isoformat(timespec="seconds"),
                           "notes": clean}, indent=2, ensure_ascii=False)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    except OSError as exc:
        ch.error(f"Could not write {target}", str(exc))
        raise typer.Exit(1) from exc
    ch.success(f"Exported {len(notes)} note{'s' if len(notes) != 1 else ''} → {target}")


@app.command("status")
def status_cmd(
    json_output: bool = typer.Option(False, "--json", help="Print the status as JSON."),
) -> None:
    """How many notes, where they live, and when the Dock last synced."""
    s = _store().stats()
    if json_output:
        _emit_json(s)
        return
    t = _table(("What", {"no_wrap": True}), ("", {"overflow": "fold"}))
    t.add_row("Notes", f"{s['notes']}  [dim](page {s['page']} · site {s['site']} · everywhere {s['everywhere']})[/dim]")
    t.add_row("Websites", str(s["sites"]))
    t.add_row("Agent may use", f"{s['agent']} note{'s' if s['agent'] != 1 else ''}" if s["agent"] else "[dim]none — kept on this computer[/dim]")
    last = s.get("last_sync") or {}
    t.add_row("Last Dock sync", f"[green]● {_when(last.get('at'))}[/green]  [dim]↑{last.get('pushed', 0)} ↓{last.get('pulled', 0)}[/dim]"
              if last else "[dim]○ never — open the NAVIG Dock in Chrome with NAVIG running[/dim]")
    t.add_row("Deleted (kept to sync)", str(s["deleted"]))
    t.add_row("Store", f"[dim]{s['db']}[/dim]")
    ch.console.print(t)
    ch.dim(f"next: {CMD} list  ·  {CMD} search <words>  ·  {CMD} add \"text\"")


__all__ = ["app"]
