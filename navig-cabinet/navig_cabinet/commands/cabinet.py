"""navig cabinet — an encrypted cabinet for important files.

House style: Rich tables plus a ``--json`` twin for scripts and agents. This module is
CLI only; the decisions live in the sibling modules (store, keys, bundle, ingest) so
they stay testable without a terminal.
"""

from __future__ import annotations

import json as _json
import os
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Annotated

import typer
from navig_sdk import console as ch
from rich.markup import escape
from rich.table import Table

from navig_sdk.host import command_name  # noqa: E402


# The command a user types here: `navig paperwork` inside navig, `navig-cabinet paperwork` on its own.
PAPERWORK_CMD = command_name("paperwork", standalone="navig-cabinet paperwork")

# The command a user types here: `navig cabinet` inside navig, `navig-cabinet` on its own.
CMD = command_name("cabinet", standalone="navig-cabinet")

cabinet_app = typer.Typer(
    name="cabinet",
    help="🗄️ An encrypted cabinet for your important files — ID and passport scans, "
         "medical records, contracts, photos, recordings. Searchable by the text inside "
         "them, with expiry reminders and portable backups. Local OCR only.",
    no_args_is_help=True,
)
passphrase_app = typer.Typer(help="Lock the cabinet with a passphrase, or go back to the machine key.",
                             no_args_is_help=True)
cabinet_app.add_typer(passphrase_app, name="passphrase")

PASSPHRASE_ENV = "NAVIG_CABINET_PASSPHRASE"
NEW_PASSPHRASE_ENV = "NAVIG_CABINET_NEW_PASSPHRASE"
BACKUP_PASSPHRASE_ENV = "NAVIG_CABINET_BACKUP_PASSPHRASE"
BACKUP_STATE = "backup.json"


# ── helpers ─────────────────────────────────────────────────────────────────


def _root() -> Path:
    from navig_cabinet.store import default_root

    return default_root()


def _fail(msg: str, code: int = 1) -> typer.Exit:
    ch.error(msg)
    return typer.Exit(code)


def _ask_passphrase(prompt: str, *, env: str, confirm: bool = False) -> str:
    value = os.environ.get(env)
    if value:
        return value
    if not sys.stdin.isatty():
        raise _fail(f"a passphrase is needed — set {env} when running without a terminal", 2)
    value = typer.prompt(prompt, hide_input=True, confirmation_prompt=confirm)
    if not value:
        raise _fail("the passphrase must not be empty", 2)
    return value


def _open_cabinet(*, create: bool = False):
    """Open (or, for `add`, create) the cabinet — or exit with a message that says why not."""
    from navig_cabinet import keys
    from navig_cabinet.store import Cabinet

    root = _root()
    if not Cabinet.exists(root):
        if not create:
            raise _fail(f"there is no cabinet yet — put something in it with `{CMD} add <file>`")
        cab = Cabinet.create(root)
        ch.info(f"Created a new cabinet at {root}, locked to this computer.")
        ch.warning(
            f"Back it up (`{CMD} backup -o <file>`) — a machine-locked cabinet cannot "
            "be opened on another computer or after a reinstall. Or set a passphrase: "
            f"`{CMD} passphrase set`."
        )
        return cab
    try:
        kf = keys.KeyFile.load(root)
        passphrase = None
        if kf.mode == "passphrase":
            passphrase = _ask_passphrase("Cabinet passphrase", env=PASSPHRASE_ENV)
        return Cabinet(root, kf.unwrap(passphrase))
    except keys.KeyError_ as exc:
        raise _fail(str(exc)) from exc


def open_cabinet(*, create: bool = False):
    """The CLI's way into the cabinet — prompts for a passphrase when one is set.

    Public for the other verbs in this plugin (`navig paperwork handoff`), so every
    entry point unlocks, creates and reports failures the same way.
    """
    return _open_cabinet(create=create)


def _resolve(cab, ref: str, *, include_trashed: bool = False):
    from navig_cabinet.store import Ambiguous, NotFound

    try:
        return cab.resolve(ref, include_trashed=include_trashed)
    except (NotFound, Ambiguous) as exc:
        raise _fail(str(exc)) from exc


def _size(n: int) -> str:
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB"):
        if n < step:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= step
    return f"{n:.1f} TB"


def _date(value: str | None) -> str | None:
    """``YYYY-MM-DD`` (or ``none`` to clear) → stored form, else a usage error."""
    if value is None:
        return None
    if value.strip().lower() in {"none", "-", ""}:
        return ""
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError as exc:
        raise _fail(f"expiry date {value!r} is not YYYY-MM-DD", 2) from exc


def _expiry_cell(item, today: date | None = None) -> str:
    days = item.days_to_expiry(today)
    if days is None:
        return "[dim]—[/dim]"
    if days < 0:
        return f"[red]✗ {item.expires} ({-days}d ago)[/red]"
    if days <= 90:
        return f"[yellow]{item.expires} ({days}d)[/yellow]"
    return item.expires or ""


def _items_table(items, *, show_state: bool = False) -> Table:
    t = Table(box=None, show_header=True, padding=(0, 2))
    t.add_column("ID", no_wrap=True, style="cyan")
    t.add_column("Title")
    t.add_column("Category", no_wrap=True)
    t.add_column("Kind", no_wrap=True, style="dim")
    t.add_column("Tags", no_wrap=True)
    t.add_column("Expires", no_wrap=True)
    t.add_column("Size", no_wrap=True, justify="right", style="dim")
    if show_state:
        t.add_column("State", no_wrap=True)
    for it in items:
        row = [it.id, escape(it.title), it.category, it.kind,
               escape(", ".join(it.tags)) or "[dim]—[/dim]", _expiry_cell(it), _size(it.size)]
        if show_state:
            row.append("[dim]trash[/dim]" if it.state == "trashed" else "active")
        t.add_row(*row)
    return t


def _emit_json(data) -> None:
    typer.echo(_json.dumps(data, ensure_ascii=False, indent=2))


def _backup_state(root: Path) -> dict:
    p = root / BACKUP_STATE
    try:
        return _json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# ── status ──────────────────────────────────────────────────────────────────


@cabinet_app.command("status")
def cmd_status(as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False):
    """What is in the cabinet, how it is locked, and what needs attention."""
    from navig_cabinet import keys
    from navig_cabinet.store import Cabinet

    root = _root()
    if not Cabinet.exists(root):
        if as_json:
            _emit_json({"exists": False, "path": str(root)})
        else:
            ch.info(f"There is no cabinet yet — put something in it with `{CMD} add <file>`.")
        return
    mode = keys.KeyFile.load(root).mode
    from navig_cabinet import reminders

    reminders_on = reminders.enabled()
    cab = _open_cabinet()
    with cab:
        items = cab.items()
        opened = cab.open_copies()
    today = date.today()
    expiring = sorted((i for i in items
                       if (d := i.days_to_expiry(today)) is not None and d <= 90),
                      key=lambda i: i.expires or "")
    backup = _backup_state(root)
    by_cat: dict[str, int] = {}
    for it in items:
        by_cat[it.category] = by_cat.get(it.category, 0) + 1
    data = {
        "exists": True, "path": str(root), "lock": mode, "items": len(items),
        "bytes": sum(i.size for i in items), "by_category": by_cat,
        "expiring_within_90_days": [i.id for i in expiring],
        "last_backup_at": backup.get("at"), "decrypted_copies_open": len(opened),
        "reminders": reminders_on,
    }
    if as_json:
        _emit_json(data)
        return

    t = Table(box=None, show_header=False, padding=(0, 2))
    t.add_column(no_wrap=True, style="dim")
    t.add_column()
    lock = ("[green]● passphrase[/green]" if mode == "passphrase"
            else "[yellow]● this computer[/yellow] [dim](no passphrase)[/dim]")
    t.add_row("Lock", lock)
    t.add_row("Items", f"{len(items)} · {_size(data['bytes'])}")
    if by_cat:
        t.add_row("By category", " · ".join(f"{k} {v}" for k, v in sorted(by_cat.items())))
    t.add_row("Expiring ≤ 90d", f"[yellow]{len(expiring)}[/yellow]" if expiring else "[dim]none[/dim]")
    t.add_row("Last backup", escape(backup.get("at", "")) or "[red]✗ never[/red]")
    t.add_row("Reminders", f"[green]● on[/green] [dim](daily after {reminders.reminder_hour()}:00 — 90/30/7 days, then expired)[/dim]"
              if reminders_on else "[dim]○ off — navig config set cabinet.reminders.enabled true[/dim]")
    if opened:
        t.add_row("Decrypted copies", f"[yellow]{len(opened)} open[/yellow] — `{CMD} close` removes them")
    t.add_row("Location", escape(str(root)))
    ch.console.print(t)
    if not backup.get("at") and items:
        ch.warning(f"Never backed up · `{CMD} backup -o <file>` makes a copy you can restore anywhere.")
    elif expiring:
        ch.info(f"Expiring soon · `{CMD} expiring`")


# ── add ─────────────────────────────────────────────────────────────────────


@cabinet_app.command("add")
def cmd_add(
    paths: Annotated[list[Path], typer.Argument(help="Files or folders to put in the cabinet.")],
    title: Annotated[str | None, typer.Option("--title", help="Title (one file only).")] = None,
    category: Annotated[str | None, typer.Option("--category", "-c",
                                                  help="identity · medical · insurance · finance · housing · legal · "
                                                       "education · vehicle · photos · recordings · other")] = None,
    tag: Annotated[list[str] | None, typer.Option("--tag", "-t", help="Tag (repeat, or comma-separate).")] = None,
    expires: Annotated[str | None, typer.Option("--expires", help="Expiry date, YYYY-MM-DD.")] = None,
    issuer: Annotated[str | None, typer.Option("--issuer", help="Who issued it.")] = None,
    notes: Annotated[str | None, typer.Option("--notes", help="Free-text notes (encrypted).")] = None,
    no_ocr: Annotated[bool, typer.Option("--no-ocr", help="Do not read the text inside the files.")] = False,
    transcribe: Annotated[bool, typer.Option("--transcribe",
                                             help="Transcribe audio/video locally so it is searchable (slow).")] = False,
    move: Annotated[bool, typer.Option("--move", help="Delete each original after it is stored and verified.")] = False,
    allow_duplicate: Annotated[bool, typer.Option("--allow-duplicate", help="Store a file even if an identical one is already in.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
):
    """Encrypt files into the cabinet (originals are kept unless --move)."""
    from navig_cabinet._core import resolve_user_path
    from navig_cabinet.categories import normalise_category, normalise_tags
    from navig_cabinet.ingest import add_path, expand
    from navig_cabinet.store import CabinetError, Duplicate

    # navig chdirs into the active space before a command runs, so a relative path must
    # be anchored to where the operator typed it — not to Path.cwd().
    files = expand([resolve_user_path(str(p)) for p in paths])
    missing = [p for p in files if not p.is_file()]
    if missing:
        raise _fail(f"not found: {', '.join(str(p) for p in missing[:5])}", 2)
    if not files:
        raise _fail("nothing to add — the folder is empty", 2)
    if title and len(files) > 1:
        raise _fail("--title applies to one file; drop it to use each file's own name", 2)
    try:
        cat = normalise_category(category) if category else None
    except ValueError as exc:
        raise _fail(str(exc), 2) from exc
    exp = _date(expires) or None
    tags = normalise_tags(tag)

    cab = _open_cabinet(create=True)
    added, dupes, failed, notes_out = [], [], [], []
    with cab:
        for f in files:
            try:
                res = add_path(cab, f, title=title, category=cat, tags=tags, expires=exp,
                               issuer=issuer, notes=notes, read_text=not no_ocr,
                               transcribe=transcribe, allow_duplicate=allow_duplicate)
            except Duplicate as exc:
                dupes.append((f, exc.existing))
                continue
            except (CabinetError, OSError) as exc:
                failed.append((f, str(exc)))
                continue
            added.append((f, res))
            notes_out.extend(f"{f.name}: {n}" for n in res.text.notes)
            if move:
                try:
                    f.unlink()
                except OSError as exc:
                    failed.append((f, f"stored, but the original could not be deleted: {exc}"))

    if as_json:
        _emit_json({
            "added": [dict(r.item.public(), source=str(f), text_chars=len(r.item.text)) for f, r in added],
            "duplicates": [{"source": str(f), "existing": e.id} for f, e in dupes],
            "failed": [{"source": str(f), "error": m} for f, m in failed],
            "notes": notes_out,
        })
    else:
        if added:
            ch.console.print(_items_table([r.item for _, r in added]))
        for f, e in dupes:
            ch.info(f"{escape(f.name)} is already in the cabinet as {e.id} ({escape(e.title)}) — skipped.")
        for n in notes_out:
            ch.warning(escape(n))
        for f, m in failed:
            ch.error(f"{escape(f.name)}: {escape(m)}")
        if added:
            verb = "moved into" if move else "stored in"
            ch.success(f"{len(added)} file(s) encrypted and {verb} the cabinet · find them with `{CMD} search <words>`")
    if failed:
        raise typer.Exit(1)


# ── list / search / show ────────────────────────────────────────────────────


@cabinet_app.command("list")
def cmd_list(
    category: Annotated[str | None, typer.Option("--category", "-c", help="Only this category.")] = None,
    tag: Annotated[str | None, typer.Option("--tag", "-t", help="Only items with this tag.")] = None,
    kind: Annotated[str | None, typer.Option("--kind", help="pdf · image · audio · video · office · text · other")] = None,
    trash: Annotated[bool, typer.Option("--trash", help="Show the trash instead.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
):
    """Everything in the cabinet."""
    from navig_cabinet.categories import normalise_category

    try:
        cat = normalise_category(category) if category else None
    except ValueError as exc:
        raise _fail(str(exc), 2) from exc
    with _open_cabinet() as cab:
        items = cab.items(include_trashed=trash)
    items = [i for i in items
             if (i.state == "trashed") == trash
             and (cat is None or i.category == cat)
             and (tag is None or tag.lower().lstrip("#") in i.tags)
             and (kind is None or i.kind == kind.lower())]
    if as_json:
        _emit_json([i.public() for i in items])
        return
    if not items:
        ch.info("Nothing here." if (cat or tag or kind or trash) else
                f"The cabinet is empty — `{CMD} add <file>`.")
        return
    ch.console.print(_items_table(items))
    ch.console.print(f"[dim]{len(items)} item(s) · {_size(sum(i.size for i in items))} · "
                     f"open one with `{CMD} open <id>`[/dim]")


def _haystack(it) -> str:
    return " ".join([it.title, it.original_name, it.category, " ".join(it.tags), it.issuer or "",
                     it.notes or "", it.text]).casefold()


def _snippet(text: str, term: str, width: int = 50) -> str:
    i = text.casefold().find(term)
    if i < 0:
        return ""
    start, end = max(0, i - width), min(len(text), i + len(term) + width)
    s = " ".join(text[start:end].split())
    return ("…" if start else "") + s + ("…" if end < len(text) else "")


@cabinet_app.command("search")
def cmd_search(
    query: Annotated[list[str], typer.Argument(help="Words to find — in titles, tags, notes and the text inside files.")],
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
):
    """Find items by title, tag, or any word inside the document."""
    terms = [t.casefold() for t in " ".join(query).split() if t.strip()]
    if not terms:
        raise _fail("give at least one word to search for", 2)
    with _open_cabinet() as cab:
        items = cab.items()
    hits = [i for i in items if all(t in _haystack(i) for t in terms)]
    if as_json:
        _emit_json([dict(i.public(), snippet=_snippet(i.text, terms[0])) for i in hits])
        return
    if not hits:
        ch.info(f"No item mentions {escape(' '.join(terms))}.")
        return
    t = Table(box=None, show_header=True, padding=(0, 2))
    t.add_column("ID", no_wrap=True, style="cyan")
    t.add_column("Title", no_wrap=True)
    t.add_column("Category", no_wrap=True)
    t.add_column("Match")
    for it in hits:
        snip = _snippet(it.text, terms[0])
        t.add_row(it.id, escape(it.title), it.category, escape(snip) if snip else "[dim]title / tags[/dim]")
    ch.console.print(t)
    ch.console.print(f"[dim]{len(hits)} match(es) · `{CMD} open <id>` · `{CMD} export <id> -o <dir>`[/dim]")


@cabinet_app.command("show")
def cmd_show(
    ref: Annotated[str, typer.Argument(help="Item id (or a unique prefix).")],
    text: Annotated[bool, typer.Option("--text", help="Print all the text read from the file.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
):
    """Details of one item."""
    with _open_cabinet() as cab:
        it = _resolve(cab, ref, include_trashed=True)
    if as_json:
        _emit_json(it.public(with_text=text))
        return
    t = Table(box=None, show_header=False, padding=(0, 2))
    t.add_column(no_wrap=True, style="dim")
    t.add_column()
    for label, value in [
        ("ID", it.id), ("Title", escape(it.title)), ("File", escape(it.original_name)),
        ("Category", it.category), ("Kind", f"{it.kind} · {it.mime}"), ("Size", _size(it.size)),
        ("Tags", escape(", ".join(it.tags)) or "—"),
        ("Expires", _expiry_cell(it) + (" [dim](read from the document)[/dim]" if it.expires_detected else "")),
        ("Issuer", escape(it.issuer or "—")), ("Notes", escape(it.notes or "—")),
        ("Added", it.added_at), ("Text", f"{len(it.text)} chars via {it.text_source}" if it.text else "—"),
        ("SHA-256", f"[dim]{it.sha256}[/dim]"),
    ] + ([("State", "[yellow]in the trash[/yellow]")] if it.state == "trashed" else []):
        t.add_row(label, value)
    ch.console.print(t)
    if it.text:
        body = it.text if text else it.text[:400] + ("…" if len(it.text) > 400 else "")
        ch.console.print()
        ch.console.print(escape(body))
        if not text and len(it.text) > 400:
            ch.console.print("[dim]--text for the rest[/dim]")
    # The extracted text has no page breaks, so it cannot be paged here. The real
    # document can: asked "how do I switch pages?" after reading a 7-page PDF this way.
    ch.console.print(
        f"[dim]Read the original, page by page: {CMD} open {it.id}  ·  "
        f"then {CMD} close[/dim]"
    )


# ── open / close / export ───────────────────────────────────────────────────


def _launch(path: Path) -> None:
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])  # noqa: S603,S607
    else:
        subprocess.Popen(["xdg-open", str(path)])  # noqa: S603,S607


@cabinet_app.command("open")
def cmd_open(ref: Annotated[str, typer.Argument(help="Item id (or a unique prefix).")]):
    """Open an item in its usual app (a temporary decrypted copy)."""
    with _open_cabinet() as cab:
        it = _resolve(cab, ref)
        path = cab.open_copy(it)
    try:
        _launch(path)
    except OSError as exc:
        raise _fail(f"decrypted to {path}, but no app would open it: {exc}") from exc
    ch.success(f"Opened {escape(it.title)}")
    ch.console.print("[dim]The decrypted copy is removed 15 min after opening (on the next cabinet "
                     f"command), or now with `{CMD} close`.[/dim]")


@cabinet_app.command("close")
def cmd_close():
    """Remove every decrypted copy left by `open` right now."""
    from navig_cabinet.store import OPEN_DIR

    d = _root() / OPEN_DIR
    left = []
    if d.is_dir():
        for p in d.iterdir():
            try:
                p.unlink()
            except OSError:
                left.append(p.name)
    if left:
        raise _fail(f"{len(left)} copy(ies) are still open in an app — close it and run this again")
    ch.success("No decrypted copies left.")


@cabinet_app.command("export")
def cmd_export(
    refs: Annotated[list[str] | None, typer.Argument(help="Item ids (or unique prefixes).")] = None,
    out: Annotated[Path, typer.Option("--out", "-o", help="Folder to write the files into.")] = Path("."),
    all_items: Annotated[bool, typer.Option("--all", help="Export everything.")] = False,
    by_category: Annotated[bool, typer.Option("--by-category", help="One subfolder per category.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
):
    """Write decrypted copies with their original names (verified; never overwrites)."""
    from navig_cabinet._core import resolve_user_path
    from navig_cabinet.store import CabinetError

    if not refs and not all_items:
        raise _fail("name the items to export, or use --all", 2)
    out_dir = resolve_user_path(str(out))
    written, failed = [], []
    with _open_cabinet() as cab:
        items = cab.items() if all_items else [_resolve(cab, r) for r in refs or []]
        for it in items:
            try:
                p = cab.export(it, out_dir, subdir=it.category if by_category else None)
                written.append((it, p))
            except (CabinetError, OSError) as exc:
                failed.append((it, str(exc)))
    if as_json:
        _emit_json({"written": [{"id": i.id, "path": str(p)} for i, p in written],
                    "failed": [{"id": i.id, "error": m} for i, m in failed]})
    else:
        for it, m in failed:
            ch.error(f"{it.id} {escape(it.title)}: {escape(m)}")
        if written:
            ch.success(f"{len(written)} file(s) exported to {escape(str(out_dir))} — each checked against its stored checksum.")
            ch.warning("These copies are NOT encrypted. Delete them when you are done.")
    if failed:
        raise typer.Exit(1)


# ── edit / remove / undelete ────────────────────────────────────────────────


@cabinet_app.command("edit")
def cmd_edit(
    ref: Annotated[str, typer.Argument(help="Item id (or a unique prefix).")],
    title: Annotated[str | None, typer.Option("--title")] = None,
    category: Annotated[str | None, typer.Option("--category", "-c")] = None,
    tag: Annotated[list[str] | None, typer.Option("--tag", "-t", help="Add a tag.")] = None,
    untag: Annotated[list[str] | None, typer.Option("--untag", help="Remove a tag.")] = None,
    expires: Annotated[str | None, typer.Option("--expires", help="YYYY-MM-DD, or `none` to clear.")] = None,
    issuer: Annotated[str | None, typer.Option("--issuer")] = None,
    notes: Annotated[str | None, typer.Option("--notes")] = None,
):
    """Change an item's title, category, tags, expiry, issuer or notes."""
    from navig_cabinet.categories import normalise_category, normalise_tags

    changes: dict = {}
    if title is not None:
        changes["title"] = title
    if category is not None:
        try:
            changes["category"] = normalise_category(category)
        except ValueError as exc:
            raise _fail(str(exc), 2) from exc
    exp = _date(expires)
    if exp is not None:
        changes["expires"] = exp or None
        changes["expires_detected"] = False
    if issuer is not None:
        changes["issuer"] = issuer or None
    if notes is not None:
        changes["notes"] = notes or None
    if not changes and not tag and not untag:
        raise _fail("nothing to change — pass --title, --category, --tag, --untag, --expires, --issuer or --notes", 2)
    with _open_cabinet() as cab:
        it = _resolve(cab, ref)
        if tag or untag:
            drop = set(normalise_tags(untag))
            changes["tags"] = [t for t in it.tags + normalise_tags(tag) if t not in drop]
            changes["tags"] = list(dict.fromkeys(changes["tags"]))
        it = cab.update(it.id, **changes)
    ch.console.print(_items_table([it]))
    ch.success("Updated.")


@cabinet_app.command("remove")
def cmd_remove(
    ref: Annotated[str, typer.Argument(help="Item id (or a unique prefix).")],
    purge: Annotated[bool, typer.Option("--purge", help="Delete for good instead of moving to the trash.")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask.")] = False,
):
    """Move an item to the trash (or --purge it for good)."""
    with _open_cabinet() as cab:
        it = _resolve(cab, ref, include_trashed=purge)
        what = "PERMANENTLY delete" if purge else "Move to the trash"
        if not yes:
            if not sys.stdin.isatty():
                raise _fail("refusing without a terminal to confirm — pass --yes", 2)
            if not typer.confirm(f"{what} {it.id} ({it.title})?", default=False):
                ch.info("Nothing changed.")
                return
        if purge:
            cab.purge(it.id)
        else:
            cab.set_state(it.id, "trashed")
    if purge:
        ch.success(f"{it.id} deleted for good.")
    else:
        ch.success(f"{it.id} is in the trash · bring it back with `{CMD} undelete {it.id}`")


@cabinet_app.command("undelete")
def cmd_undelete(ref: Annotated[str, typer.Argument(help="Item id (or a unique prefix).")]):
    """Bring an item back from the trash."""
    with _open_cabinet() as cab:
        it = _resolve(cab, ref, include_trashed=True)
        if it.state != "trashed":
            ch.info(f"{it.id} is not in the trash.")
            return
        cab.set_state(it.id, "active")
    ch.success(f"{it.id} ({escape(it.title)}) is back.")


# ── expiry ──────────────────────────────────────────────────────────────────


@cabinet_app.command("expiring")
def cmd_expiring(
    within: Annotated[int, typer.Option("--within", help="Days ahead to look.")] = 90,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
):
    """Documents that have expired or expire soon — passports, ID cards, insurance."""
    today = date.today()
    with _open_cabinet() as cab:
        items = cab.items()
    due = sorted((i for i in items if (d := i.days_to_expiry(today)) is not None and d <= within),
                 key=lambda i: i.expires or "")
    if as_json:
        _emit_json([dict(i.public(), days_left=i.days_to_expiry(today)) for i in due])
        return
    if not due:
        dated = sum(1 for i in items if i.expires)
        ch.info(f"Nothing expires in the next {within} days ({dated} item(s) have an expiry date · "
                f"add one with `{CMD} edit <id> --expires YYYY-MM-DD`).")
        return
    ch.console.print(_items_table(due))
    overdue = sum(1 for i in due if (i.days_to_expiry(today) or 0) < 0)
    ch.console.print(f"[dim]{len(due)} item(s) · {overdue} already expired[/dim]")


# ── backup / restore ────────────────────────────────────────────────────────


@cabinet_app.command("backup")
def cmd_backup(
    out: Annotated[Path, typer.Option("--out", "-o", help="Backup file to write (.ncab).")],
    with_vault: Annotated[bool, typer.Option(
        "--with-vault", help="Also seal every vault secret (API keys, passwords, tokens) into the file.")] = False,
):
    """Write one encrypted backup file that restores on any computer, with its own passphrase.

    With --with-vault it also carries the navig vault, whose own storage only opens on
    this machine — the one file then holds everything personal.
    """
    from navig_cabinet._core import resolve_user_path
    from navig_cabinet.bundle import BackupError, recovery_script, write_backup
    from navig_cabinet.store import CabinetError
    from navig_cabinet.vault_bridge import VaultBridgeError, export_vault

    dest = resolve_user_path(str(out))
    if dest.suffix != ".ncab":
        dest = dest.with_name(dest.name + ".ncab")
    if dest.exists():
        raise _fail(f"{dest} already exists — choose another name", 2)
    dest.parent.mkdir(parents=True, exist_ok=True)
    cab = _open_cabinet(create=with_vault)
    with cab:
        items = cab.items()
        vault = None
        if with_vault:
            try:
                vault = export_vault()
            except (VaultBridgeError, OSError) as exc:
                raise _fail(f"could not read the vault: {exc}") from exc
            if vault.unreadable:
                # Unreadable here means unreadable anywhere: say so rather than ship a
                # backup the operator believes is complete.
                ch.warning(f"{len(vault.unreadable)} vault item(s) cannot be decrypted on this "
                           f"machine and are NOT in the backup: {escape(', '.join(vault.unreadable))}")
        if not items and not (vault and vault.entries):
            raise _fail("nothing to back up — the cabinet is empty"
                        + ("" if with_vault else " (add --with-vault to back up the vault)"), 2)
        passphrase = _ask_passphrase("Backup passphrase (you will need it to restore)",
                                     env=BACKUP_PASSPHRASE_ENV, confirm=True)
        tmp = dest.with_name(dest.name + ".part")
        try:
            with tmp.open("wb") as f:
                n = write_backup(cab, items, f, passphrase,
                                 vault_entries=vault.entries if vault else None)
            os.replace(tmp, dest)
        except (BackupError, CabinetError, OSError) as exc:
            tmp.unlink(missing_ok=True)
            raise _fail(f"backup failed: {exc}") from exc
    state = {"at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(), "items": n}
    if vault:
        state["vault_items"] = len(vault.entries)
    (cab.root / BACKUP_STATE).write_text(_json.dumps(state), encoding="utf-8")
    carried = f" + {len(vault.entries)} vault secret(s)" if vault else ""
    ch.success(f"{n} item(s){carried} backed up → {escape(str(dest))} ({_size(dest.stat().st_size)})")
    if vault:
        ch.console.print("[dim]This file now holds your secrets: keep it somewhere safe, and "
                         "treat its passphrase like the keys it protects.[/dim]")
    ch.console.print(f"[dim]Restore: `{CMD} restore {escape(dest.name)}` · without navig: "
                     f"{escape(str(recovery_script()))}[/dim]")


@cabinet_app.command("restore")
def cmd_restore(
    src: Annotated[Path, typer.Argument(help="A .ncab backup file.")],
    skip_vault: Annotated[bool, typer.Option(
        "--skip-vault", help="Do not restore vault secrets the backup carries.")] = False,
):
    """Add everything from a backup that the cabinet does not already hold.

    A backup made with --with-vault also restores the vault's secrets. Existing items
    and secrets are never overwritten — this machine's copy wins.
    """
    from navig_cabinet._core import resolve_user_path
    from navig_cabinet.bundle import BackupError, restore_backup
    from navig_cabinet.vault_bridge import VaultBridgeError, import_vault

    path = resolve_user_path(str(src))
    if not path.is_file():
        raise _fail(f"{path} not found", 2)
    passphrase = _ask_passphrase("Backup passphrase", env=BACKUP_PASSPHRASE_ENV)
    cab = _open_cabinet(create=True)
    with cab, path.open("rb") as f:
        try:
            st = restore_backup(cab, f, passphrase)
        except BackupError as exc:
            raise _fail(str(exc)) from exc
    vst = None
    if st.vault_entries is not None and not skip_vault:
        try:
            vst = import_vault(st.vault_entries)
        except (VaultBridgeError, OSError) as exc:
            raise _fail(f"documents restored, but the vault could not be: {exc}") from exc
    errors = st.errors + (vst.errors if vst else [])
    for e in errors:
        ch.warning(escape(e))
    ch.success(f"Restored {st.restored} item(s) · {st.skipped} already present.")
    if vst:
        ch.success(f"Vault: restored {vst.restored} secret(s) · {vst.skipped} already present.")
    elif st.vault_entries is not None:
        ch.info(f"The backup carries {len(st.vault_entries)} vault secret(s) — skipped (--skip-vault).")
    if errors:
        raise typer.Exit(1)


# ── paperwork handoff ───────────────────────────────────────────────────────


@cabinet_app.command("import-paperwork")
def cmd_import_paperwork(
    manifest: Annotated[Path | None, typer.Argument(help="A paperwork handoff.jsonl (default: --space's).")] = None,
    space: Annotated[str | None, typer.Option("--space", help=f"The space `{PAPERWORK_CMD} scan` ran in.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be imported.")] = False,
    no_ocr: Annotated[bool, typer.Option("--no-ocr", help="Do not read the text inside the files.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
):
    """Encrypt the ID and medical documents a `navig paperwork` scan set aside (originals stay put)."""
    from navig_cabinet._core import resolve_user_path
    from navig_cabinet.ingest import import_paperwork
    from navig_cabinet.store import CabinetError

    if manifest is None:
        if not space:
            raise _fail("give the handoff.jsonl path, or --space <name>", 2)
        from navig_cabinet.paperwork.space import paths_for

        try:
            path = paths_for(space).handoff_jsonl
        except ValueError as exc:
            raise _fail(str(exc), 2) from exc
    else:
        path = resolve_user_path(str(manifest))
    if not path.is_file():
        raise _fail(f"no handoff manifest at {path} — run `{PAPERWORK_CMD} scan` first", 2)

    from navig_cabinet.store import Cabinet

    cab = None if dry_run and not Cabinet.exists(_root()) else _open_cabinet(create=True)
    try:
        report = import_paperwork(cab, path, read_text=not no_ocr, dry_run=dry_run)
    except CabinetError as exc:
        raise _fail(str(exc)) from exc
    finally:
        if cab is not None:
            cab.close()
    if as_json:
        _emit_json([r.__dict__ for r in report.rows])
    else:
        style = {"imported": "[green]● imported[/green]", "already": "[dim]○ already in[/dim]",
                 "skipped": "[dim]· not personal[/dim]", "missing": "[yellow]⚠ missing[/yellow]",
                 "changed": "[yellow]⚠ changed[/yellow]", "error": "[red]✗ error[/red]"}
        t = Table(box=None, show_header=True, padding=(0, 2))
        t.add_column("Result", no_wrap=True)
        t.add_column("ID", no_wrap=True, style="cyan")
        t.add_column("File")
        t.add_column("Detail", no_wrap=True)
        for r in report.rows:
            if r.outcome == "skipped":
                continue
            t.add_row(style.get(r.outcome, r.outcome), r.item_id or "—", escape(Path(r.src).name), escape(r.detail))
        ch.console.print(t)
        verb = "would be imported" if dry_run else "imported"
        ch.success(f"{report.count('imported')} {verb} · {report.count('already')} already in · "
                   f"{report.count('skipped')} not personal · originals untouched.")
    if report.count("error"):
        raise typer.Exit(1)


# ── verify ──────────────────────────────────────────────────────────────────


@cabinet_app.command("dates")
def cmd_dates(
    apply: Annotated[bool, typer.Option("--apply", help="Save the dates found (they are marked as read from the document).")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
):
    """Find expiry dates inside documents that have none — passports, ID cards, policies.

    Reads only what a document states about itself: the machine-readable zone of a
    passport or ID card (check-digit verified), or a labelled "date d'expiration" /
    "date of expiry". Correct any of them with `navig cabinet edit <id> --expires`.
    """
    from navig_cabinet.dates import find_expiry

    found = []
    with _open_cabinet() as cab:
        for it in cab.items():
            if it.expires or not it.text:
                continue
            hit = find_expiry(it.text)
            if hit:
                found.append((it, hit))
        if apply:
            for it, hit in found:
                cab.update(it.id, expires=hit.expires, expires_detected=True)
    if as_json:
        _emit_json([{"id": it.id, "title": it.title, "category": it.category,
                     "expires": hit.expires, "source": hit.source} for it, hit in found])
        return
    if not found:
        ch.info("No expiry date found in documents that lack one.")
        return
    t = Table(box=None, show_header=True, padding=(0, 2))
    t.add_column("ID", no_wrap=True, style="cyan")
    t.add_column("Title")
    t.add_column("Category", no_wrap=True)
    t.add_column("Expires", no_wrap=True)
    t.add_column("From", no_wrap=True, style="dim")
    for it, hit in sorted(found, key=lambda x: x[1].expires):
        t.add_row(it.id, escape(it.title), it.category, hit.expires,
                  "machine-readable zone" if hit.source == "mrz" else "labelled date")
    ch.console.print(t)
    if apply:
        ch.success(f"Saved {len(found)} expiry date(s) — reminders now cover them. "
                   f"Fix any with `{CMD} edit <id> --expires YYYY-MM-DD`.")
    else:
        ch.info(f"{len(found)} date(s) found · save them with `{CMD} dates --apply`")


@cabinet_app.command("remind")
def cmd_remind(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be sent; send nothing.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
):
    """Send the expiry reminders that are due now (the daemon also does this daily).

    A reminder names the category and the id, never the title — it may be read on a
    phone lock screen. Each threshold (90/30/7 days, then expired) is sent once per
    document and expiry date.
    """
    import asyncio
    from datetime import datetime

    from navig_cabinet import reminders

    root = _root()
    with _open_cabinet() as cab:           # refreshes the index from the real catalog
        from navig_cabinet.reminders import write_index

        write_index(cab.root, cab.items())
    if not reminders.enabled():
        raise _fail("reminders are off — navig config set cabinet.reminders.enabled true", 2)
    rows = reminders.read_index(root)
    state = reminders._load_state(root)
    batch = reminders.due(rows, dict(state.get("sent") or {}), datetime.now().date())
    if dry_run or not batch:
        title, body = reminders.message(batch) if batch else ("", "")
        if as_json:
            _emit_json({"due": len(batch), "title": title, "body": body, "sent": 0})
        elif batch:
            ch.info(f"Would send: {escape(title)}")
            ch.console.print(escape(body))
        else:
            ch.success("Nothing is due — every dated document is outside the 90-day window "
                       "or has already been reminded.")
        return
    result = asyncio.run(reminders.check(root, force=True))
    if as_json:
        _emit_json(result)
    elif result["sent"]:
        ch.success(f"Sent {result['sent']} reminder(s) through your notification channels.")
    if result.get("pending"):
        raise _fail("the reminder could not be delivered — it will be retried; check "
                    "Settings → Notifications")


@cabinet_app.command("verify")
def cmd_verify(as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False):
    """Decrypt every item and check it against its stored checksum."""
    from navig_cabinet.store import CabinetError

    bad = []
    with _open_cabinet() as cab:
        items = cab.items(include_trashed=True)
        for it in items:
            try:
                if not cab.verify(it):
                    bad.append((it, "does not decrypt to its checksum"))
            except (CabinetError, OSError) as exc:
                bad.append((it, str(exc)))
    if as_json:
        _emit_json({"checked": len(items), "bad": [{"id": i.id, "error": m} for i, m in bad]})
    elif bad:
        for it, m in bad:
            ch.error(f"{it.id} {escape(it.title)}: {escape(m)}")
    else:
        ch.success(f"All {len(items)} item(s) decrypt and match their checksums.")
    if bad:
        raise typer.Exit(1)


# ── passphrase ──────────────────────────────────────────────────────────────


@passphrase_app.command("set")
def cmd_passphrase_set():
    """Lock the cabinet with a passphrase (asked for on every command)."""
    from navig_cabinet import keys

    with _open_cabinet() as cab:
        new = _ask_passphrase("New cabinet passphrase", env=NEW_PASSPHRASE_ENV, confirm=True)
        keys.set_passphrase(cab.root, cab.master_key, new)
    ch.success("The cabinet now needs your passphrase. Nothing was re-encrypted — only the key changed.")
    ch.warning("If you forget it, the files cannot be recovered. A backup has its own passphrase.")


@passphrase_app.command("clear")
def cmd_passphrase_clear():
    """Go back to the machine key (no passphrase)."""
    from navig_cabinet import keys

    with _open_cabinet() as cab:
        keys.clear_passphrase(cab.root, cab.master_key)
    ch.success("The cabinet opens on this computer without a passphrase again.")
