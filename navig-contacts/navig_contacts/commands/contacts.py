"""``navig contacts`` — one address book.

This used to be two things. Core carried an alias→route table for message
dispatch: who is ``@alice`` and where does a message to her go. Nothing carried
the other half — the people themselves, merged out of whatever exports had piled
up, with every number, address, handle and spelling they have ever had.

They are now the same command over the same store, because they were always the
same question asked at two depths. A route is a *transport*; an identifier is the
*address itself*, and an address belongs to exactly one person — which is what
makes it the key that decides two records are one human.

The rule the address-book half turns on: **a contact is legit only if it has a
phone number**, and "has a phone number" means the number validates. Everything
weaker is a lead, filed at a tier that says so.

Two books exist and ``--space`` chooses between them. The default is the global
one at ``~/.navig/data/contacts.db`` — what this command has always meant, and
what ``navig dispatch send`` resolves against.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from navig_sdk import console as ch
from navig_sdk.host import command_name

# The command a user types here: `navig contacts` inside navig, `navig-contacts` on its own.
CMD = command_name("contacts")

app = typer.Typer(
    name="contacts",
    help="👥  Your address book: import, merge, search, and route messages.",
    no_args_is_help=True,
)

TIERS = ("verified", "email", "handle", "archive")

_space_opt = typer.Option(
    None, "--space", "-s",
    help="Use this space's contact book instead of the global one.",
)


def _open(space: Optional[str]):
    """Point the engine at a book and return (engine_db_module, ContactStore)."""
    from navig_contacts.store import ContactStore

    from navig_contacts.engine import db as db_mod

    if space:
        try:
            db_mod.use_book(db_mod.resolve_space_dir(space) / db_mod.BOOK_FILENAME)
        except ValueError as exc:
            ch.error(str(exc))
            raise typer.Exit(2) from exc
    else:
        db_mod.use_book(None)
    try:
        return db_mod, ContactStore(db_path=db_mod.book_path())
    except Exception as exc:
        from navig_contacts.engine.migrate import inspect

        if inspect(db_mod.book_path()).needed:
            ch.error(f"{db_mod.book_path()} predates the merged contact "
                     f"book and cannot be read yet.")
            ch.dim(f"Migrate it: {CMD} migrate"
                   + (f" --space {space}" if space else "") + " --apply")
            raise typer.Exit(1) from exc
        raise


def _empty(db_mod, what: str = "contacts") -> None:
    """Say which book was read, so an empty one is never mistaken for the other."""
    ch.warning(f"No {what} in {db_mod.describe_book()}.")
    other = ("Try --space <name> for a space's own book."
             if db_mod.book_path() == db_mod.global_book_path()
             else "Drop --space to read the global book.")
    ch.dim(other)


# ── the routing half: alias -> where a message goes ───────────


@app.command("list")
def list_cmd(
    space: Optional[str] = _space_opt,
    tier: Optional[str] = typer.Option(
        None, "--tier", "-t",
        help="verified | email | handle | archive. 'verified' is the set with a "
             "phone number that validated.",
    ),
    limit: int = typer.Option(200, "--limit", "-n", help="Maximum to display."),
    plain: bool = typer.Option(False, "--plain", help="Tab-separated output."),
) -> None:
    """List contacts."""
    if tier and tier not in TIERS:
        ch.error(f"Unknown tier {tier!r}. One of: {', '.join(TIERS)}")
        raise typer.Exit(2)

    db_mod, _store = _open(space)
    sql = ("SELECT alias, display_name, tier, primary_phone, default_network, city "
           "FROM contacts WHERE COALESCE(is_deleted, 0) = 0 AND merged_into IS NULL")
    params: list = []
    if tier:
        sql += " AND tier = ?"
        params.append(tier)
    sql += " ORDER BY display_name COLLATE NOCASE, alias LIMIT ?"
    params.append(limit)

    with db_mod.get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
        routes = _routes_for(conn, [r["alias"] for r in rows])

    if not rows:
        _empty(db_mod)
        return

    if plain:
        for r in rows:
            ch.console.print(
                f"{r['alias']}\t{r['display_name'] or ''}\t{r['primary_phone'] or ''}"
                f"\t{r['tier'] or ''}\t{','.join(routes.get(r['alias'], []))}"
            )
        return

    from rich.table import Table

    table = Table(title=f"Contacts — {db_mod.describe_book()}")
    table.add_column("Alias", style="cyan", no_wrap=True)
    table.add_column("Name")
    table.add_column("Phone", style="green", no_wrap=True)
    table.add_column("Tier", style="dim")
    table.add_column("Routes", style="magenta")
    for r in rows:
        table.add_row(
            f"@{r['alias']}", r["display_name"] or "—",
            r["primary_phone"] or "—", r["tier"] or "—",
            ", ".join(routes.get(r["alias"], [])) or "—",
        )
    ch.console.print(table)
    ch.dim(f"{len(rows)} shown")


def _routes_for(conn, aliases: list[str]) -> dict[str, list[str]]:
    """Routes for exactly the listed contacts.

    Bounded on purpose: an unfiltered join pulls every route in the book to
    render one page of twenty.
    """
    if not aliases:
        return {}
    out: dict[str, list[str]] = {}
    placeholders = ", ".join("?" * len(aliases))
    for row in conn.execute(
        f"SELECT c.alias, r.network, r.address FROM contact_routes r "
        f"JOIN contacts c ON c.id = r.contact_id "
        f"WHERE c.alias IN ({placeholders}) ORDER BY r.priority", aliases
    ):
        out.setdefault(row["alias"], []).append(
            f"{row['network']}:{row['address']}")
    return out


@app.command("add")
def add_cmd(
    alias: str = typer.Option(..., "--alias", "-a", help="Unique contact alias."),
    name: Optional[str] = typer.Option(None, "--name", "-N", help="Display name."),
    route: Optional[list[str]] = typer.Option(
        None, "--route", "-r", help="network:address (repeatable)."),
    default_network: Optional[str] = typer.Option(
        None, "--default", "-d", help="Default network."),
    phone: Optional[str] = typer.Option(
        None, "--phone", "-p", help="Phone number (shorthand for --route sms:<phone>)."),
    space: Optional[str] = _space_opt,
) -> None:
    """Add a contact by hand.

    Examples:
        navig contacts add --alias alice --name 'Alice B.' --route 'whatsapp:+33612345678'
        navig contacts add --alias bob --phone +1234567890 --route 'discord:bob#1234'
    """
    _db_mod, store = _open(space)
    alias_clean = alias.lstrip("@")
    if store.resolve_alias(alias_clean) is not None:
        ch.error(f"Contact @{alias_clean} already exists.")
        raise typer.Exit(1)

    route_strings = list(route or [])
    for r in route_strings:
        if ":" not in r:
            ch.error(f"Invalid route format {r!r} — expected 'network:address'.")
            raise typer.Exit(2)
    if phone:
        # The plugin's normaliser, not core's: this one has libphonenumber
        # behind it, so '+666' is refused rather than stored.
        from navig_contacts.engine.identity import normalize_phone

        normalised, why = normalize_phone(phone)
        if not normalised:
            ch.error(f"Not a usable phone number: {phone!r} — {why}")
            raise typer.Exit(2)
        route_strings.append(f"sms:{normalised}")

    contact = store.add_contact(
        alias=alias_clean, display_name=name or "",
        routes=route_strings, default_network=default_network,
    )
    ch.success(f"Contact @{contact.alias} added ({len(route_strings)} routes)")


@app.command("route")
def route_cmd(
    alias: str = typer.Argument(..., help="Contact alias."),
    action: str = typer.Argument(..., help="add | remove"),
    route_spec: str = typer.Argument(..., help="network:address"),
    priority: Optional[int] = typer.Option(
        None, "--priority", "-p",
        help="Route priority. Omitted means one past the highest this contact "
             "has, so the newest route does not silently tie with the others."),
    space: Optional[str] = _space_opt,
) -> None:
    """Add or remove a route for a contact."""
    if action not in ("add", "remove"):
        ch.error(f"Unknown action {action!r} — expected 'add' or 'remove'.")
        raise typer.Exit(2)
    if ":" not in route_spec:
        ch.error(f"Invalid route format {route_spec!r} — expected 'network:address'.")
        raise typer.Exit(2)

    _db_mod, store = _open(space)
    alias_clean = alias.lstrip("@")
    if store.resolve_alias(alias_clean) is None:
        ch.error(f"Contact @{alias_clean} not found.")
        raise typer.Exit(1)

    if action == "add":
        before = {(r.network, r.address)
                  for r in store.resolve_alias(alias_clean).routes}
        store.add_route(alias_clean, route_spec, priority=priority)
        after = {(r.network, r.address)
                 for r in store.resolve_alias(alias_clean).routes}
        if after == before:
            # add_route uses INSERT OR IGNORE and reports success either way;
            # saying "added" for a route that was already there is a lie the
            # operator would act on.
            ch.warning(f"@{alias_clean} already had {route_spec} — nothing changed.")
            return
        ch.success(f"Route {route_spec} added to @{alias_clean}.")
        return

    if not store.remove_route(alias_clean, route_spec):
        ch.error(f"@{alias_clean} has no route {route_spec}.")
        raise typer.Exit(1)
    ch.success(f"Route {route_spec} removed from @{alias_clean}.")


@app.command("remove")
def remove_cmd(
    alias: str = typer.Argument(..., help="Contact alias."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
    space: Optional[str] = _space_opt,
) -> None:
    """Remove a contact."""
    _db_mod, store = _open(space)
    alias_clean = alias.lstrip("@")
    if store.resolve_alias(alias_clean) is None:
        ch.error(f"Contact @{alias_clean} not found.")
        raise typer.Exit(1)
    if not yes and not typer.confirm(f"Remove @{alias_clean}?"):
        ch.dim("Cancelled.")
        return
    if not store.remove_contact(alias_clean):
        ch.error(f"Contact @{alias_clean} was not removed — the store rejected "
                 f"the write.")
        raise typer.Exit(1)
    ch.success(f"Contact @{alias_clean} removed.")


# ── the address-book half: who these people are ───────────────


@app.command("show")
def show_cmd(
    alias: str = typer.Argument(..., help="Contact alias."),
    space: Optional[str] = _space_opt,
) -> None:
    """Everything known about one contact, and where it came from."""
    db_mod, _store = _open(space)
    alias_clean = alias.lstrip("@")

    with db_mod.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM contacts WHERE alias = ? COLLATE NOCASE "
            "AND COALESCE(is_deleted, 0) = 0", (alias_clean,)
        ).fetchone()
        if row is None:
            moved = conn.execute(
                "SELECT s.alias FROM contacts v JOIN contacts s ON s.id = v.merged_into "
                "WHERE v.alias = ? COLLATE NOCASE", (alias_clean,)
            ).fetchone()
            if moved:
                ch.error(f"@{alias_clean} was merged into @{moved[0]}.")
                raise typer.Exit(1)
            ch.error(f"Contact @{alias_clean} not found.")
            raise typer.Exit(1)

        routes = conn.execute(
            "SELECT network, address, priority, meta_json FROM contact_routes "
            "WHERE contact_id = ? ORDER BY priority", (row["id"],)).fetchall()
        idents = conn.execute(
            "SELECT kind, value_norm, is_primary FROM contact_identifiers "
            "WHERE contact_id = ? ORDER BY is_primary DESC, kind", (row["id"],)
        ).fetchall()
        aliases = [a[0] for a in conn.execute(
            "SELECT name FROM contact_aliases WHERE contact_id = ? ORDER BY name",
            (row["id"],))]
        sources = conn.execute(
            "SELECT source_file, COUNT(*) FROM contact_sources "
            "WHERE contact_id = ? GROUP BY source_file", (row["id"],)).fetchall()

    ch.console.print(f"  Alias:   @{row['alias']}")
    ch.console.print(f"  Name:    {row['display_name'] or '(none)'}")
    if _col(row, "tier"):
        ch.console.print(f"  Tier:    {row['tier']}"
                         + ("   (a phone number that validated)"
                            if row["tier"] == "verified" else ""))
    for field, label in (("primary_phone", "Phone"), ("primary_email", "Email"),
                         ("org", "Org"), ("job_title", "Title"),
                         ("birthday", "Born"), ("city", "City")):
        if _col(row, field):
            ch.console.print(f"  {label + ':':<9}{row[field]}")
    ch.console.print(f"  Default: {row['default_network'] or '(auto)'}")
    from navig_contacts.engine.liveness import DEAD_STATES, verdict_of

    for r in routes:
        state, checked = verdict_of(_col(r, "meta_json"))
        # An unchecked route says nothing, which is not the same as saying it
        # works -- so only a real verdict is printed.
        note = ""
        if state in DEAD_STATES:
            note = f"  · {state}" + (f", checked {checked}" if checked else "")
        elif state == "ok":
            note = f"  · resolved{f' {checked}' if checked else ''}"
        ch.console.print(f"  Route:   {r['network']}:{r['address']}  "
                         f"(priority={r['priority']}){note}")
    for i in idents:
        ch.console.print(f"  {i['kind'] + ':':<9}{i['value_norm']}"
                         + ("  (primary)" if i["is_primary"] else ""))
    others = [a for a in aliases if a != (row["display_name"] or "")]
    if others:
        ch.console.print(f"  Also:    {', '.join(others)}")
    for src, n in sources:
        ch.console.print(f"  From:    {n} record(s) in {src}")


def _col(row, name: str):
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


@app.command("import")
def import_cmd(
    path: Path = typer.Argument(
        ..., help="A .vcf file, a folder of them, or a Telegram contacts.json / export."),
    space: Optional[str] = _space_opt,
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what would happen; write nothing."),
    source: str = typer.Option(
        "import", "--source", help="Label recorded against every row."),
    reports: bool = typer.Option(
        True, "--reports/--no-reports",
        help="Write the merge, rejection and interests reports beside the book."),
) -> None:
    """Import an address book, merging duplicates into one contact per person.

    Detects the format: a .vcf file or a folder of them goes through the vCard
    importer; a Telegram contacts.json or export ZIP goes through Telegram's.
    """
    if not path.exists():
        ch.error(f"Not found: {path}")
        raise typer.Exit(2)

    kind = _detect_format(path)
    if kind is None:
        ch.error(f"Cannot tell what {path.name} is — expected a .vcf file, a "
                 f"folder containing .vcf files, or a Telegram contacts.json/ZIP.")
        raise typer.Exit(2)

    if kind == "telegram":
        _import_telegram(path, space, dry_run)
        return
    if kind == "telegram-bot":
        _import_telegram_bot(path, space, dry_run, source)
        return
    _import_vcf(path, space, dry_run, source, reports)


def _detect_format(path: Path) -> Optional[str]:
    """
    Work out what kind of export this is by looking inside it.

    A Telegram Desktop export can be two different things and they are not
    interchangeable: `contacts.json` is your address book, while a *chat*
    export's `result.json` is a conversation — and when that conversation is
    with a matchmaking bot, the profile cards in it are contacts nothing else
    can read.
    """
    if path.is_dir():
        if any(path.glob("*.vcf")) or any(path.glob("*.vcard")):
            return "vcf"
        if any(path.glob("messages*.html")):
            return "telegram-bot"
        return None
    suffix = path.suffix.lower()
    if suffix in (".vcf", ".vcard"):
        return "vcf"
    if suffix == ".zip":
        return "telegram"
    if suffix == ".json":
        return "telegram-bot" if _looks_like_a_chat_export(path) else "telegram"
    return None


def _looks_like_a_chat_export(path: Path) -> bool:
    """True for a chat export (has `messages`), false for a contacts list."""
    import json

    try:
        with path.open(encoding="utf-8") as handle:
            head = handle.read(4096)
    except OSError:
        return False
    if '"messages"' in head:
        return True
    if '"contacts"' in head or '"about"' in head:
        return False
    try:
        with path.open(encoding="utf-8") as handle:
            return isinstance(json.load(handle).get("messages"), list)
    except (OSError, ValueError):
        return False


def _import_telegram(path: Path, space: Optional[str], dry_run: bool) -> None:
    try:
        from navig.importers.core import UniversalImporter
    except ImportError:
        ch.error("Importing a Telegram export needs navig (pip install navig).",
                 "vCard files import without it.")
        raise typer.Exit(1) from None
    from navig_contacts.store import normalize_phone

    _db_mod, store = _open(space)
    imported = UniversalImporter().run_one("telegram", path=str(path))
    if not imported:
        ch.warning("No Telegram contacts found in that export.")
        return

    added = skipped = 0
    for item in imported:
        alias = item.label.lower().replace(" ", "_")[:32]
        if store.resolve_alias(alias) is not None:
            skipped += 1
            continue
        if dry_run:
            added += 1
            continue
        phone = normalize_phone(item.value)
        contact = store.add_contact(
            alias=alias, display_name=item.label,
            routes=[f"sms:{phone}"] if phone else [])
        # Without this the row has no tier at all, and `list --tier` — the
        # question this command exists to answer — cannot see it.
        _classify(store, contact.alias, phone)
        added += 1
    verb = "would add" if dry_run else "added"
    ch.success(f"Telegram: {verb} {added}, {skipped} already known.")


def _classify(store, alias: str, phone: str) -> None:
    """Record a freshly added contact's tier and phone."""
    import sqlite3

    conn = sqlite3.connect(store.db_path, timeout=5.0)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            "UPDATE contacts SET tier = ?, primary_phone = ? WHERE alias = ?",
            ("verified" if phone else "handle", phone or None, alias))
        conn.commit()
    finally:
        conn.close()


def _import_telegram_bot(path: Path, space: Optional[str], dry_run: bool,
                         source: str) -> None:
    """A chat export with a matchmaking bot: the profile cards are contacts."""
    db_mod, _store = _open(space)
    from navig_contacts.engine.backup import auto_backup
    from navig_contacts.engine.telegram_bot import (
        import_bot_export, parse_telegram_export, parse_telegram_html_export)

    if dry_run:
        profiles = (parse_telegram_html_export(path) if path.is_dir()
                    else parse_telegram_export(path))
        with_card = sum(1 for p in profiles if p.get("age"))
        with_photo = sum(1 for p in profiles if p.get("photo_rel_path"))
        ch.info(f"{len(profiles)} profile(s) in the chat export")
        ch.console.print(f"    {with_card} with an age and city, "
                         f"{with_photo} with a photo")
        ch.warning("Dry run — nothing was written.")
        return

    ch.dim(f"Backup: {auto_backup('pre-import')}")
    base = db_mod.base_dir()
    with db_mod.get_db() as conn:
        summary = import_bot_export(path, conn, source=source,
                                    photos_dir=base / "photos")
    ch.success(f"{summary.inserted} added, {summary.duplicates} already known, "
               f"{summary.errors} errors (of {summary.total} profiles)")
    if summary.photos_matched:
        # Not "copied": `_copy_photo` returns the existing path when the file is
        # already there, so a re-import reported hundreds of copies that never
        # happened. It counts profiles whose photo is on disk, so say that.
        ch.dim(f"{summary.photos_matched} profile(s) have a photo on file.")
    if summary.photos_unmatched:
        ch.dim(f"{summary.photos_unmatched} photo(s) named in the export are "
               f"missing from it.")


def _import_vcf(path: Path, space: Optional[str], dry_run: bool,
                source: str, reports: bool) -> None:
    db_mod, _store = _open(space)
    from navig_contacts.engine.backup import auto_backup
    from navig_contacts.engine.runs import finish_run, new_session_id, start_run
    from navig_contacts.engine.vcf_import import (
        analyse, import_vcf, reviewed_aliases, write_reports)

    base = db_mod.base_dir()
    if dry_run:
        result = analyse(path)
        _print_tiers(result)
        if reports:
            with db_mod.get_db() as conn:
                seen = reviewed_aliases(conn)
            written = write_reports(result, base / "exports", seen)
            ch.dim(f"{len(written)} reports refreshed in {base / 'exports'}. "
                   f"Nothing was written to the book.")
        return

    ch.dim(f"Backup: {auto_backup('pre-import')}")
    session = new_session_id()
    run_id, _before = start_run(source, str(path), kind="vcf", session_id=session)
    with db_mod.get_db() as conn:
        result = import_vcf(path, conn, session, source_label=source,
                            photos_dir=base / "photos")
    finish_run(run_id, result.summary)

    _print_tiers(result)
    s = result.summary
    ch.success(f"run #{run_id}: {s.inserted} added, {s.duplicates} already known, "
               f"{s.errors} errors")
    if result.photos and result.photos.extracted:
        ch.dim(f"{result.photos.extracted} embedded avatars extracted.")
    if reports:
        with db_mod.get_db() as conn:
            seen = reviewed_aliases(conn)
        written = write_reports(result, base / "exports", seen)
        ch.dim(f"{len(written)} reports in {base / 'exports'}. Read the review "
               f"one before trusting the merges.")


def _print_tiers(result) -> None:
    counts = result.tier_counts()
    ch.info(f"{result.cards} records in {len(result.files)} file(s) "
            f"-> {len(result.clusters)} distinct people")
    ch.console.print(f"    verified {counts['verified']:>6}  "
                     f"(a phone that validated — the real contacts)")
    ch.console.print(f"    email    {counts['email']:>6}  (email only)")
    ch.console.print(f"    handle   {counts['handle']:>6}  (an account handle only)")
    ch.console.print(f"    archive  {counts['archive']:>6}  "
                     f"(no way to reach them — not imported)")
    ch.dim(f"    {len(result.rejected)} phone values rejected, "
           f"{len(result.assumed)} needed an assumed region")


@app.command("search")
def search_cmd(
    query: str = typer.Argument(..., help="Name, alias, phone, email or handle."),
    space: Optional[str] = _space_opt,
) -> None:
    """Find someone by anything you remember about them."""
    db_mod, _store = _open(space)
    from navig_contacts.engine.identity import name_key, normalize_phone

    like = f"%{query}%"
    key = f"%{name_key(query)}%" if name_key(query) else None
    e164, _reason = normalize_phone(query)
    ident = (e164 or query).strip().lower()

    with db_mod.get_db() as conn:
        rows = conn.execute(
            """SELECT DISTINCT c.alias, c.display_name, c.tier, c.primary_phone, c.city
                 FROM contacts c
                 LEFT JOIN contact_aliases a ON a.contact_id = c.id
                 LEFT JOIN contact_identifiers i ON i.contact_id = c.id
                 LEFT JOIN contact_routes r ON r.contact_id = c.id
                WHERE COALESCE(c.is_deleted, 0) = 0 AND c.merged_into IS NULL
                  AND (c.display_name LIKE ? OR c.alias = ? OR a.name LIKE ?
                       OR (? IS NOT NULL AND a.name_key LIKE ?)
                       OR i.value_norm = ? OR r.address = ?)
                ORDER BY c.display_name COLLATE NOCASE LIMIT 50""",
            (like, query.lstrip("@"), like, key, key, ident, ident),
        ).fetchall()

    if not rows:
        ch.warning(f"Nothing in {db_mod.describe_book()} matches {query!r}.")
        return

    from rich.table import Table

    table = Table(title=f"Matches for {query!r}")
    table.add_column("Alias", style="cyan", no_wrap=True)
    table.add_column("Name")
    table.add_column("Phone", style="green", no_wrap=True)
    table.add_column("Tier", style="dim")
    table.add_column("City", style="dim")
    for r in rows:
        table.add_row(f"@{r['alias']}", r["display_name"] or "—",
                      r["primary_phone"] or "—", r["tier"] or "—", r["city"] or "—")
    ch.console.print(table)
    ch.dim(f"{len(rows)} result(s)")


@app.command("merge")
def merge_cmd(
    alias: str = typer.Argument(..., help="The contact to absorb."),
    into: str = typer.Argument(..., help="The contact that survives."),
    space: Optional[str] = _space_opt,
) -> None:
    """Fold one contact into another, reversibly."""
    if alias.lstrip("@") == into.lstrip("@"):
        ch.error("A contact cannot be merged into itself.")
        raise typer.Exit(2)

    db_mod, _store = _open(space)
    from navig_contacts.engine.backup import auto_backup
    from navig_contacts.engine.merge import absorb

    with db_mod.get_db() as conn:
        victim = conn.execute("SELECT id FROM contacts WHERE alias = ?",
                              (alias.lstrip("@"),)).fetchone()
        survivor = conn.execute("SELECT id FROM contacts WHERE alias = ?",
                                (into.lstrip("@"),)).fetchone()
    if victim is None:
        ch.error(f"Contact @{alias.lstrip('@')} not found.")
        raise typer.Exit(1)
    if survivor is None:
        ch.error(f"Contact @{into.lstrip('@')} not found.")
        raise typer.Exit(1)

    ch.dim(f"Backup: {auto_backup('pre-merge')}")
    with db_mod.get_db() as conn:
        absorb(conn, survivor[0], victim[0], "manual", "merged by hand")
    ch.success(f"@{alias.lstrip('@')} merged into @{into.lstrip('@')}. "
               f"Undo: {CMD} undo <id> (see: {CMD} merges)")


@app.command("split")
def split_cmd(
    alias: str = typer.Argument(..., help="The contact to split."),
    identifier: str = typer.Argument(..., metavar="KIND:VALUE",
                                     help="e.g. phone:+33671514812"),
    name: Optional[str] = typer.Option(
        None, "--as", help="Display name for the contact this creates."),
    space: Optional[str] = _space_opt,
) -> None:
    """Move one identifier onto a contact of its own.

    For when a record listed somebody else's number — the failure an automatic
    import actually makes, and the one `undo` cannot help with, because no merge
    was ever logged: the records were read as one person from the start.
    """
    db_mod, _store = _open(space)
    from navig_contacts.engine.backup import auto_backup
    from navig_contacts.engine.merge import split_identifier

    with db_mod.get_db() as conn:
        row = conn.execute("SELECT id FROM contacts WHERE alias = ?",
                           (alias.lstrip("@"),)).fetchone()
    if row is None:
        ch.error(f"Contact @{alias.lstrip('@')} not found.")
        raise typer.Exit(1)

    ch.dim(f"Backup: {auto_backup('pre-split')}")
    with db_mod.get_db() as conn:
        outcome = split_identifier(conn, alias.lstrip("@"), identifier, name)

    if outcome.status == "bad_spec":
        ch.error("Expected KIND:VALUE, e.g. phone:+33671514812 or email:a@b.com.")
        raise typer.Exit(2)
    if outcome.status == "not_held":
        ch.error(f"@{alias.lstrip('@')} does not hold {identifier}.")
        ch.dim(f"It holds: {outcome.detail or 'nothing'}")
        raise typer.Exit(1)
    if outcome.status == "would_strand":
        ch.error(f"@{alias.lstrip('@')} would be left with no way to reach them. "
                 f"Edit or remove the contact instead.")
        raise typer.Exit(1)
    if outcome.status == "taken":
        ch.error(f"A contact already holds the alias @{outcome.detail}.")
        raise typer.Exit(1)

    ch.success(f"{identifier} now belongs to @{outcome.detail}; "
               f"@{alias.lstrip('@')} keeps the rest.")


@app.command("merges")
def merges_cmd(
    space: Optional[str] = _space_opt,
    limit: int = typer.Option(40, "--limit", "-n"),
) -> None:
    """Every merge and split applied, and whether it was undone."""
    db_mod, _store = _open(space)
    with db_mod.get_db() as conn:
        rows = conn.execute(
            "SELECT m.id, m.created_at, m.merged_alias, m.reason, m.undone_at, "
            "c.alias AS survivor FROM merge_log m "
            "LEFT JOIN contacts c ON c.id = m.surviving_id "
            "ORDER BY m.id DESC LIMIT ?", (limit,)).fetchall()
    if not rows:
        _empty(db_mod, "merges")
        return
    for r in rows:
        state = "undone" if r["undone_at"] else "applied"
        ch.console.print(f"  #{r['id']:<4} {(r['created_at'] or '')[:16]}  "
                         f"{r['merged_alias']} -> {r['survivor']}  ·  {state}"
                         f"  ·  {r['reason']}")


@app.command("undo")
def undo_cmd(
    merge_id: int = typer.Argument(..., help=f"Id from `{CMD} merges`."),
    space: Optional[str] = _space_opt,
) -> None:
    """Reverse a merge, restoring the absorbed contact."""
    db_mod, _store = _open(space)
    from navig_contacts.engine.backup import auto_backup
    from navig_contacts.engine.merge import undo_merge

    ch.dim(f"Backup: {auto_backup('pre-merge-undo')}")
    with db_mod.get_db() as conn:
        outcome = undo_merge(conn, merge_id)

    if outcome.status == "missing":
        ch.error(f"No merge #{merge_id}.")
        raise typer.Exit(1)
    if outcome.status == "already_undone":
        ch.warning(f"Merge #{merge_id} was already undone at {outcome.detail}.")
        return
    if outcome.status == "unreadable":
        ch.error(f"Merge #{merge_id} has no readable snapshot; undo it by hand.")
        raise typer.Exit(1)

    ch.success(f"Merge #{merge_id} undone: restored @{outcome.detail} with "
               f"{outcome.moved} identifier(s).")
    if outcome.blocked:
        ch.warning("Left with the surviving contact (an address belongs to one "
                   f"person): {', '.join(outcome.blocked)}")


@app.command("export")
def export_cmd(
    space: Optional[str] = _space_opt,
    tier: Optional[str] = typer.Option(None, "--tier", "-t", help="Filter by tier."),
    output: Optional[Path] = typer.Option(
        None, "--output", "-o", help="Where to write. Default: stdout."),
    include_dead: bool = typer.Option(
        False, "--include-dead",
        help="Keep contacts whose every route is flagged dead."),
) -> None:
    """Export contacts as CSV.

    Contacts whose routes have *all* been flagged dead are left out; pass
    `--include-dead` to keep them. Someone whose old handle died but whose
    phone still works is reachable and always exported.
    """
    if tier and tier not in TIERS:
        ch.error(f"Unknown tier {tier!r}. One of: {', '.join(TIERS)}")
        raise typer.Exit(2)

    db_mod, _store = _open(space)
    import csv
    import io

    sql = ("SELECT alias, display_name, tier, primary_phone, primary_email, org, "
           "birthday, city, country, default_network, source_profile "
           "FROM contacts WHERE COALESCE(is_deleted, 0) = 0 AND merged_into IS NULL")
    params: list = []
    if tier:
        sql += " AND tier = ?"
        params.append(tier)
    if not include_dead:
        from navig_contacts.engine.liveness import unreachable_sql
        sql += f" AND NOT ({unreachable_sql()})"
    sql += " ORDER BY display_name COLLATE NOCASE"

    with db_mod.get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
        skipped = 0 if include_dead else conn.execute(
            _count_unreachable(tier), [tier] if tier else []).fetchone()[0]
    if not rows:
        _empty(db_mod)
        return

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(rows[0].keys())
    for r in rows:
        writer.writerow(["" if v is None else v for v in tuple(r)])

    if output is None:
        typer.echo(buf.getvalue(), nl=False)
        if skipped:
            # stderr, not stdout: the CSV is being piped somewhere and a note
            # in the middle of it would be parsed as a row.
            typer.echo(f"{skipped} unreachable contact(s) left out "
                       f"(--include-dead keeps them).", err=True)
        return
    output.write_text(buf.getvalue(), encoding="utf-8")
    ch.success(f"{len(rows)} contacts -> {output}")
    if skipped:
        ch.dim(f"{skipped} unreachable contact(s) left out "
               f"(--include-dead keeps them).")


def _count_unreachable(tier: Optional[str]) -> str:
    """How many the export just dropped -- silence would hide a bad sweep."""
    from navig_contacts.engine.liveness import unreachable_sql

    sql = ("SELECT COUNT(*) FROM contacts WHERE COALESCE(is_deleted, 0) = 0 "
           "AND merged_into IS NULL")
    if tier:
        sql += " AND tier = ?"
    return sql + f" AND ({unreachable_sql()})"


@app.command("stats")
def stats_cmd(space: Optional[str] = _space_opt) -> None:
    """How many people, at which tiers, and how much evidence backs them."""
    db_mod, _store = _open(space)
    with db_mod.get_db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM contacts WHERE COALESCE(is_deleted, 0) = 0 "
            "AND merged_into IS NULL").fetchone()[0]
        tiers = conn.execute(
            "SELECT tier, COUNT(*) FROM contacts WHERE COALESCE(is_deleted, 0) = 0 "
            "AND merged_into IS NULL GROUP BY tier ORDER BY 2 DESC").fetchall()
        idents = conn.execute(
            "SELECT kind, COUNT(*) FROM contact_identifiers "
            "GROUP BY kind ORDER BY 2 DESC").fetchall()
        routes = conn.execute("SELECT COUNT(*) FROM contact_routes").fetchone()[0]
        aliases = conn.execute("SELECT COUNT(*) FROM contact_aliases").fetchone()[0]
        records = conn.execute("SELECT COUNT(*) FROM contact_sources").fetchone()[0]

    if not total:
        _empty(db_mod)
        return
    ch.info(f"{total} contacts in {db_mod.describe_book()}")
    for tier, n in tiers:
        ch.console.print(f"  {tier or 'unclassified':<12} {n}")
    ch.console.print(f"  {routes} routes, {aliases} name variants, "
                     f"{records} source records")
    for kind, n in idents:
        ch.console.print(f"  {kind:<12} {n}")


@app.command("migrate")
def migrate_cmd(
    space: Optional[str] = _space_opt,
    apply: bool = typer.Option(
        False, "--apply",
        help="Actually migrate. Without it this only reports what it would do."),
) -> None:
    """Bring a pre-merge contact book onto the shared schema.

    A space that ran the older space-local tool has a book keyed on `uid` with
    handles in their own table. This renames the columns in place and turns the
    handles into identifiers and routes; nothing is rebuilt and nothing is lost.
    """
    db_mod, _store = _open_raw(space)
    from navig_contacts.engine.migrate import inspect, migrate

    path = db_mod.book_path()
    plan = inspect(path)
    if not plan.needed:
        ch.success(f"{path} — {plan.reason}. Nothing to do.")
        return

    ch.info(f"{path}")
    ch.info(f"  {plan.reason}")
    ch.info(f"  {plan.contacts} contacts")
    for step in plan.steps:
        ch.console.print(f"    · {step}")

    if not apply:
        ch.warning("Nothing was changed. Re-run with --apply to migrate.")
        return

    from navig_contacts.engine.backup import auto_backup

    ch.dim(f"Backup: {auto_backup('pre-schema-migration')}")
    migrate(path)
    ch.success(f"Migrated. `{CMD} list"
               f"{' --space ' + space if space else ''}` now reads it.")


def _open_raw(space: Optional[str]):
    """Point the engine at a book WITHOUT creating the schema.

    `migrate` has to look at a book the shared store cannot open yet, so it
    cannot go through _open(), which would raise on the old column names.
    """
    from navig_contacts.engine import db as db_mod

    if space:
        try:
            db_mod.use_book(db_mod.resolve_space_dir(space) / db_mod.BOOK_FILENAME)
        except ValueError as exc:
            ch.error(str(exc))
            raise typer.Exit(2) from exc
    else:
        db_mod.use_book(None)
    return db_mod, None


@app.command("review")
def review_cmd(
    alias: Optional[str] = typer.Argument(
        None, help="Contact whose flagged merge you have judged correct."),
    space: Optional[str] = _space_opt,
    all_flagged: bool = typer.Option(
        False, "--all", help="Mark every currently-flagged contact as judged."),
) -> None:
    """Record that a flagged merge was looked at and found correct.

    The review report is regenerated on every import. Without a record of what
    has already been judged it reprints the same verdicts every time, and a
    genuinely new suspicious merge arrives buried among them.
    """
    if bool(alias) == all_flagged:
        ch.error("Give a contact alias, or --all. Not both, not neither.")
        raise typer.Exit(2)

    db_mod, _store = _open(space)
    from navig_contacts.engine.merge import currently_flagged

    with db_mod.get_db() as conn:
        targets = [alias.lstrip("@")] if alias else currently_flagged(conn)
        if not targets:
            ch.success("Nothing flagged — every merge has been judged.")
            return
        marked, missing = 0, []
        for target in targets:
            cur = conn.execute(
                "UPDATE contacts SET merge_reviewed_at = datetime('now') "
                "WHERE alias = ? COLLATE NOCASE AND merge_reviewed_at IS NULL",
                (target,))
            if cur.rowcount:
                marked += 1
            elif not conn.execute(
                    "SELECT 1 FROM contacts WHERE alias = ? COLLATE NOCASE",
                    (target,)).fetchone():
                missing.append(target)

    if missing:
        ch.error(f"No such contact: {', '.join(missing)}")
        raise typer.Exit(1)
    ch.success(f"{marked} merge(s) marked as judged. The review report will not "
               f"reprint them; a newly suspicious merge still will.")
    ch.dim(f"Changed your mind: `{CMD} split` clears the mark.")


@app.command("flag")
def flag_cmd(
    handle: Optional[str] = typer.Option(
        None, "--handle", "-H", help="One handle to judge (with or without @)."),
    state: str = typer.Option(
        "gone", "--state", help="The verdict. One of: ok, gone, invalid, not_a_user."),
    from_file: Optional[Path] = typer.Option(
        None, "--from-file",
        help='JSONL, one {"handle": "x", "state": "gone"} per line.'),
    network: str = typer.Option(
        "telegram", "--network", "-n", help="Which transport the handle is on."),
    space: Optional[str] = _space_opt,
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what would change; write nothing."),
) -> None:
    """Record whether a handle still resolves, so exports can skip dead ones.

    A username that no longer exists is not someone you can reach, but nothing
    says so on its own -- so every export re-emits it and every run spends a
    lookup rediscovering it. This writes the answer down once.

    
      ok          resolves to a real person
      gone        does not resolve (deleted or renamed)
      invalid     malformed, or rejected by the platform
      not_a_user  resolves, but to a channel or a group

    The verdict goes on the *route*, not the person: someone whose old handle
    died is still reachable by phone, and `export` only drops a contact when
    every route it has is dead.
    """
    from navig_contacts.engine.liveness import (
        MEANING, STATES, read_verdicts, record,
    )

    if state not in STATES:
        ch.error(f"Unknown state {state!r}. One of: {', '.join(STATES)}")
        for name, meaning in MEANING.items():
            ch.console.print(f"    {name:<12}{meaning}")
        raise typer.Exit(2)
    if bool(handle) == bool(from_file):
        ch.error("Pass exactly one of --handle or --from-file.")
        raise typer.Exit(2)

    if handle:
        pairs = [(handle.lstrip("@"), state)]
    else:
        try:
            pairs = read_verdicts(from_file)
        except (OSError, ValueError) as exc:
            ch.error(str(exc))
            raise typer.Exit(2) from exc
        if not pairs:
            ch.warning(f"{from_file.name} holds no verdicts.")
            return

    db_mod, _store = _open(space)
    checked = now_stamp()
    touched = unknown = 0
    unknown_handles: list[str] = []

    with db_mod.get_db() as conn:
        for one, verdict in pairs:
            n = conn.execute(
                "SELECT COUNT(*) FROM contact_routes WHERE network = ? COLLATE NOCASE "
                "AND address = ? COLLATE NOCASE", (network, one)).fetchone()[0]
            if not n:
                unknown += 1
                unknown_handles.append(one)
                continue
            if not dry_run:
                n = record(conn, one, verdict, network=network, checked_at=checked)
            touched += n
        if dry_run:
            conn.rollback()

    verb = "would flag" if dry_run else "flagged"
    ch.success(f"{verb} {touched} route(s) in {db_mod.describe_book()}")
    if unknown:
        # Named, not just counted: a typo'd handle and a handle that genuinely
        # is not in the book look identical in a total.
        ch.warning(f"{unknown} handle(s) have no {network} route here:")
        for one in unknown_handles[:10]:
            ch.console.print(f"    @{one}")
        if unknown > 10:
            ch.console.print(f"    ... and {unknown - 10} more")
    if dry_run:
        ch.dim("Dry run -- nothing was written.")


@app.command("liveness")
def liveness_cmd(space: Optional[str] = _space_opt) -> None:
    """What the book knows about which routes still resolve."""
    from navig_contacts.engine.liveness import MEANING, counts, unreachable_sql

    db_mod, _store = _open(space)
    with db_mod.get_db() as conn:
        tally = counts(conn)
        stranded = conn.execute(
            "SELECT COUNT(*) FROM contacts WHERE COALESCE(is_deleted, 0) = 0 "
            f"AND merged_into IS NULL AND ({unreachable_sql()})").fetchone()[0]

    ch.info(f"Route liveness in {db_mod.describe_book()}")
    for name, meaning in MEANING.items():
        ch.console.print(f"  {tally[name]:>6}  {name:<12}{meaning}")
    ch.console.print(f"  {tally['unchecked']:>6}  unchecked   "
                     f"never looked at -- not the same as dead")
    if stranded:
        ch.console.print("")
        ch.warning(f"{stranded} contact(s) have no live route left. "
                   f"`export` leaves them out; --include-dead keeps them.")


def now_stamp() -> str:
    """One timestamp for a whole sweep, so a batch reads as a single pass."""
    from navig_contacts.engine.liveness import now

    return now()


@app.command("doctor")
def doctor_cmd(
    space: Optional[str] = _space_opt,
    fix: bool = typer.Option(
        False, "--fix", help="Repair what can be repaired without a judgement call."),
) -> None:
    """Check that the book says the same thing in both of its halves.

    Every data bug this book has had was one shape: two things that should
    describe each other, drifting apart unnoticed. None of them raise — they sit
    there being wrong — so something has to go and look.
    """
    db_mod, _store = _open(space)
    from navig_contacts.engine.doctor import diagnose, repair

    with db_mod.get_db() as conn:
        findings = diagnose(conn)

    if not findings:
        ch.success(f"{db_mod.describe_book()} — nothing disagrees.")
        return

    total = sum(f.count for f in findings)
    ch.warning(f"{len(findings)} kind(s) of disagreement, {total} row(s), "
               f"in {db_mod.describe_book()}:")
    for finding in findings:
        ch.console.print(f"  {finding.count:>6}  {finding.check}")
        ch.console.print(f"          {finding.detail}")
        for row in finding.rows[:5]:
            ch.console.print(f"            {row}")
        if len(finding.rows) > 5:
            ch.console.print(f"            … and {finding.count - 5} more")

    if not fix:
        fixable = [f for f in findings if f.fix]
        if fixable:
            ch.dim(f"Repair {len(fixable)} of them: {CMD} doctor --fix"
                   + (f" --space {space}" if space else ""))
        return

    from navig_contacts.engine.backup import auto_backup

    ch.dim(f"Backup: {auto_backup('pre-doctor')}")
    with db_mod.get_db() as conn:
        done = repair(conn)
        remaining = diagnose(conn)

    for what, n in done.items():
        if n:
            ch.console.print(f"  {n:>6}  {what.replace('_', ' ')}")
    if remaining:
        ch.warning(f"{sum(f.count for f in remaining)} row(s) still disagree — "
                   f"those need a decision, not a repair.")
    else:
        ch.success("Nothing disagrees now.")


def register() -> None:  # pragma: no cover - registration shim
    """No-op: this plugin registers through its module def, not here."""
    return None
