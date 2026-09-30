"""navig paperwork — file business paperwork into a space.

House style: Rich tables plus a ``--json`` twin for scripts and agents. This module is
CLI only; every decision lives in the sibling modules, so the verbs stay readable and
the heuristics stay testable without a terminal.

The flow is deliberately two-step. ``scan`` reads the source drives and writes a plan;
``apply`` reads only that plan. Between them the plan is an ordinary CSV a person can
open, correct and hand back — which is the review gate, and a better one than a prompt
loop when the corpus is hundreds of files.
"""

from __future__ import annotations

import json as _json
from pathlib import Path
from typing import Annotated

import typer
from navig_sdk import console as ch
from rich.table import Table

from navig_sdk.host import command_name  # noqa: E402


# The command a user types here: `navig paperwork` inside navig, `navig-cabinet paperwork` on its own.
PAPERWORK_CMD = command_name("paperwork", standalone="navig-cabinet paperwork")

# The command a user types here: `navig cabinet` inside navig, `navig-cabinet` on its own.
CMD = command_name("cabinet", standalone="navig-cabinet")

paperwork_app = typer.Typer(
    name="paperwork",
    help="📄 File paperwork into a space — business (invoices, quotes, contracts, tax) or "
         "personal mail (CAF, CPAM, bail, préfecture, EDF…). Content-classified, deduped, "
         "hash-verified, reversible. Local OCR only.",
    no_args_is_help=True,
)

_DECISION_STYLE = {
    "migrate": "[green]● migrate[/green]",
    "review": "[yellow]⚠ review[/yellow]",
    "handoff": "[magenta]→ handoff[/magenta]",
    "trash-dupe": "[dim]○ duplicate[/dim]",
    "skip": "[dim]· skip[/dim]",
}


def _fmt_decision(d: str) -> str:
    return _DECISION_STYLE.get(d, d)


def _paths(space: str):
    """Resolve the space, turning an unknown name into a usage error."""
    from navig_cabinet.paperwork.space import paths_for

    try:
        return paths_for(space)
    except ValueError as exc:
        ch.error(str(exc))
        raise typer.Exit(2) from exc


def _profile(paths, explicit: str | None) -> str:
    """Resolve the profile, turning an unknown name into a usage error."""
    from navig_cabinet.paperwork.profiles import resolve_profile

    try:
        return resolve_profile(paths.space_root, explicit)
    except ValueError as exc:
        ch.error(str(exc))
        raise typer.Exit(2) from exc


def _run_apply(
    paths,
    rows,
    plan_path: Path,
    *,
    dry_run: bool,
    only: str | None,
    limit: int | None,
    yes: bool,
    as_json: bool,
    send: bool,
) -> None:
    """The body of `apply`, shared with `scan --apply`."""
    from navig_cabinet.paperwork import mailroom
    from navig_cabinet.paperwork.apply import apply_plan
    from navig_cabinet.paperwork.plan import summarize

    counts = summarize(rows)
    actionable = counts.get("migrate", 0) + counts.get("trash-dupe", 0)

    if not actionable:
        ch.warning(f"Nothing to apply in {plan_path.name}.")
        ch.dim(f"Decisions present: {counts or 'none'}")
        return

    if not (yes or dry_run):
        ch.info(f"{actionable} row(s) ready: {counts}")
        ch.dim("Nothing was changed. Re-run with --yes to apply, or --dry-run to rehearse.")
        return

    stats, receipt = apply_plan(rows, paths, dry_run=dry_run, only=only, limit=limit)

    # Personal profile: the mailroom keeps a ledger of every letter filed and the
    # deadlines it carries. Business rows are untouched by this (they have no
    # courrier-* class), so the company space never grows a mailroom by accident.
    filed, n_ledger, n_due = mailroom.record_filed(rows, paths, receipt)

    telegram: bool | None = None
    if send and filed and not dry_run:
        from navig_cabinet._core import notify_operator

        telegram = notify_operator(mailroom.telegram_filed_text(filed))

    payload = {
        "dry_run": dry_run,
        "migrated": stats.migrated,
        "skipped": stats.skipped,
        "duplicates_quarantined": stats.trashed_dupe,
        "left_for_review": stats.review,
        "handoff": stats.handoff,
        "mismatch": stats.mismatch,
        "collision": stats.collision,
        "receipt": str(receipt) if receipt else None,
        "errors": stats.errors,
        "courrier_ledger_new": n_ledger,
        "echeances_new": n_due,
        "telegram": telegram,
    }
    if as_json:
        ch.console.print_json(_json.dumps(payload, default=str))
    else:
        verb = "Would migrate" if dry_run else "Migrated"
        ch.success(f"{verb} {stats.migrated} · duplicates quarantined {stats.trashed_dupe} · "
                   f"already filed {stats.skipped} · left for review {stats.review}")
        if stats.handoff:
            ch.info(f"{stats.handoff} document(s) belong to another space — recorded in the manifest, untouched.")
        if filed:
            mp = mailroom.mailroom_paths(paths)
            ch.info(f"Mailroom: {n_ledger} new ledger entr{'y' if n_ledger == 1 else 'ies'}, "
                    f"{n_due} new deadline(s) → {mp.base}")
        for err in stats.errors[:20]:
            ch.warning(err)
        if receipt:
            ch.dim(f"Receipt → {receipt}   (reverse with `{PAPERWORK_CMD} undo`)")
        if telegram is True:
            ch.dim("Telegram: sent.")
        elif telegram is False:
            ch.warning("Telegram: not sent (no chat configured or the Bot API refused).")

    if stats.failed:
        ch.error(
            f"{stats.mismatch} verification failure(s), {stats.collision} collision(s) — "
            "sources were kept."
        )
        raise typer.Exit(1)


def _load_plan(paths, plan: Path | None):
    from navig_cabinet.paperwork.plan import read_csv

    path = plan or paths.plan_csv
    if not path.exists():
        ch.error(f"no plan at {path} — run `{PAPERWORK_CMD} scan <source>` first")
        raise typer.Exit(2)
    try:
        return read_csv(path), path
    except OSError as exc:
        ch.error(f"cannot read {path}: {exc}")
        raise typer.Exit(2) from exc


@paperwork_app.command("scan")
def cmd_scan(
    sources: Annotated[list[Path], typer.Argument(help="Folders or files to scan.")],
    space: Annotated[str, typer.Option("--space", help="Destination space.")] = "company",
    out: Annotated[Path | None, typer.Option("--out", help="Plan CSV path.")] = None,
    ocr: Annotated[bool, typer.Option("--ocr/--no-ocr", help="Read scanned pages via local OCR.")] = True,
    cloud_ocr: Annotated[bool, typer.Option("--cloud-ocr", help="Allow a paid cloud vision provider. Off by default: this reads tax, bank and medical documents.")] = False,
    max_pages: Annotated[int, typer.Option("--max-pages", help="PDF pages to read per document.")] = 3,
    all_types: Annotated[bool, typer.Option("--all-types", help="Consider every file type, not just documents. Slow: sweeps in image and audio caches.")] = False,
    limit: Annotated[int | None, typer.Option("--limit", help="Stop after N documents.")] = None,
    profile: Annotated[str | None, typer.Option("--profile", help="business | personal. Default: the space's `paperwork.profile`, else business.")] = None,
    apply: Annotated[bool, typer.Option("--apply", help="Apply the plan right away (needs --yes). For unattended runs.")] = False,
    yes: Annotated[bool, typer.Option("--yes", help="With --apply: actually move files.")] = False,
    send: Annotated[bool, typer.Option("--send", help="With --apply: one Telegram line per filed letter.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable summary.")] = False,
) -> None:
    """Extract, classify and deduplicate documents into a reviewable plan.

    Under the personal profile (`--profile personal`, or the space's config) photos,
    scans and PDFs of letters are read with local OCR and filed under
    `personal/<bucket>/`, with émetteur, échéance and action recorded in the mailroom.
    """
    from navig_cabinet.paperwork import courrier, handoff
    from navig_cabinet.paperwork.extract import IMAGE_SUFFIXES, missing_extractors, ocr_unavailable_reason
    from navig_cabinet.paperwork.plan import scan, stamp, summarize, write_csv

    paths = _paths(space)
    prof = _profile(paths, profile)
    # `scan inbox --space X` means the space's own inbox/: a relative source that does
    # not exist from the cwd is retried against the space root (cron jobs and the
    # Telegram intake run from arbitrary directories).
    sources = [
        (paths.space_root / s) if (not s.is_absolute() and not s.exists() and (paths.space_root / s).exists()) else s
        for s in sources
    ]
    missing = [s for s in sources if not s.exists()]
    if missing:
        ch.error(f"source not found: {', '.join(str(m) for m in missing)}")
        raise typer.Exit(2)
    if apply and not (yes or as_json):
        ch.error("--apply moves files: add --yes.")
        raise typer.Exit(2)

    # Refuse to produce a confident-looking plan we cannot actually justify. Without a
    # PDF reader every document is classified on its filename alone, which is exactly
    # how a medical bill named `facture-….pdf` ends up in finance/invoices/.
    absent = missing_extractors()
    if absent:
        ch.error("cannot read documents — missing: " + ", ".join(absent))
        ch.dim("Install with: pip install pypdf PyMuPDF python-docx  (OCR also needs tesseract on PATH)")
        raise typer.Exit(2)

    mark = stamp()
    plan_path = out or paths.plan_csv
    if plan_path.exists():
        archived = paths.archived_plan(mark)
        plan_path.replace(archived)
        ch.dim(f"previous plan archived → {archived.name}")

    seen = 0

    def progress(n: int, path: Path) -> None:
        nonlocal seen
        seen = n
        if n % 50 == 0:
            ch.dim(f"  scanned {n}… {path.name[:60]}")

    channels = courrier.load_channels(paths.space_root) if prof == "personal" else None
    if prof == "personal" and ocr:
        # Photographed letters are the point of this profile; say so up front when
        # they cannot be read, instead of filing every photo as "review".
        reason = ocr_unavailable_reason()
        has_images = any(
            s.is_file() and s.suffix.lower() in IMAGE_SUFFIXES
            or (s.is_dir() and any(f.suffix.lower() in IMAGE_SUFFIXES for f in s.rglob("*") if f.is_file()))
            for s in sources
        )
        if reason and has_images:
            ch.warning(f"images will not be read — local OCR unavailable: {reason}")

    ch.info(f"Scanning {len(sources)} source(s) → {paths.space_root.name} ({prof})")
    rows = scan(
        [Path(s) for s in sources],
        ocr=ocr,
        cloud=cloud_ocr,
        max_pages=max_pages,
        all_types=all_types,
        limit=limit,
        on_progress=progress,
        evidence_out=paths.scan_jsonl(mark),
        profile=prof,
        channels=channels,
    )

    if not rows:
        # Not a failure: an empty source is a legitimate answer.
        if as_json:
            ch.console.print_json(_json.dumps({"scanned": 0, "plan": None, "handoff": 0, "decisions": {}}))
        else:
            ch.info(f"No documents found under {', '.join(str(s) for s in sources)}.")
        return

    write_csv(rows, plan_path)
    n_handoff = handoff.write(rows, paths.handoff_jsonl, paths.handoff_md)
    counts = summarize(rows)

    if as_json and not apply:
        ch.console.print_json(_json.dumps(
            {"scanned": len(rows), "plan": str(plan_path), "handoff": n_handoff,
             "decisions": counts, "profile": prof},
            default=str,
        ))
        return

    if not as_json:
        table = Table(title=f"Scanned {len(rows)} document(s) — {prof}")
        table.add_column("Decision")
        table.add_column("Count", justify="right")
        for decision, count in counts.items():
            table.add_row(_fmt_decision(decision), str(count))
        ch.console.print(table)
        if prof == "personal":
            letters = Table(title="Courrier")
            for col in ("Decision", "Émetteur", "Objet / fichier", "Échéance", "Action", "Destination"):
                letters.add_column(col, overflow="fold")
            for r in rows[:100]:
                letters.add_row(
                    _fmt_decision(r.decision), courrier.organism_label(r.emetteur) or r.emetteur or "—",
                    (r.objet or Path(r.src).name)[:60], r.echeance or "—", r.action_requise or "—",
                    r.dest_rel or (r.notes or "—"),
                )
            ch.console.print(letters)
        ch.success(f"Plan written → {plan_path}")
        if n_handoff:
            ch.info(
                f"{n_handoff} document(s) belong to another space — recorded in {paths.handoff_md.name}, "
                "not moved, not filed."
            )

    if apply:
        _run_apply(paths, rows, plan_path, dry_run=not yes, only=None, limit=None,
                   yes=yes, as_json=as_json, send=send)
    elif not as_json:
        ch.dim(f"Review with `{PAPERWORK_CMD} review --needs-review`, then `{PAPERWORK_CMD} apply --yes`.")


@paperwork_app.command("review")
def cmd_review(
    space: Annotated[str, typer.Option("--space")] = "company",
    plan: Annotated[Path | None, typer.Option("--plan")] = None,
    needs_review: Annotated[bool, typer.Option("--needs-review", help="Only rows awaiting a decision.")] = False,
    show_handoff: Annotated[bool, typer.Option("--handoff", help="Only non-company documents.")] = False,
    dupes: Annotated[bool, typer.Option("--dupes", help="Only duplicate groups.")] = False,
    doc_class: Annotated[str | None, typer.Option("--class", help="Filter by document class.")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show what the plan intends, or just the rows that still need you."""
    paths = _paths(space)
    rows, path = _load_plan(paths, plan)

    selected = rows
    if needs_review:
        selected = [r for r in selected if r.decision == "review"]
    if show_handoff:
        selected = [r for r in selected if r.decision == "handoff"]
    if dupes:
        selected = [r for r in selected if r.dup_group or r.near_group]
    if doc_class:
        selected = [r for r in selected if r.doc_class == doc_class]

    if as_json:
        ch.console.print_json(_json.dumps(
            [{"row_id": r.row_id, "decision": r.decision, "doc_class": r.doc_class,
              "confidence": r.confidence, "src": r.src, "dest_rel": r.dest_rel,
              "signals": r.signals, "notes": r.notes} for r in selected],
            default=str,
        ))
        return

    if not selected:
        # An empty selection is an answer, not an error — often the good one.
        ch.info(f"Nothing matches in {path.name} ({len(rows)} row(s) total).")
        return

    table = Table(title=f"{len(selected)} of {len(rows)} row(s) — {path.name}")
    table.add_column("Decision")
    table.add_column("Class")
    table.add_column("Conf", justify="right")
    table.add_column("Document", overflow="fold")
    table.add_column("Destination", overflow="fold")
    for r in selected[:200]:
        table.add_row(
            _fmt_decision(r.decision),
            r.doc_class,
            f"{r.confidence:.2f}",
            Path(r.src).name,
            r.dest_rel or (r.notes or "—"),
        )
    ch.console.print(table)
    if len(selected) > 200:
        ch.dim(f"… {len(selected) - 200} more; use --json for the full list.")


@paperwork_app.command("apply")
def cmd_apply(
    space: Annotated[str, typer.Option("--space")] = "company",
    plan: Annotated[Path | None, typer.Option("--plan")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Report what would happen; touch nothing.")] = False,
    only: Annotated[str | None, typer.Option("--only", help="Restrict to one document class.")] = None,
    limit: Annotated[int | None, typer.Option("--limit")] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Required to actually move files.")] = False,
    send: Annotated[bool, typer.Option("--send", help="Personal profile: one Telegram line per filed letter.")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Execute the plan: copy, verify by hash, then quarantine the source."""
    paths = _paths(space)
    rows, path = _load_plan(paths, plan)
    _run_apply(paths, rows, path, dry_run=dry_run, only=only, limit=limit,
               yes=yes, as_json=as_json, send=send)


@paperwork_app.command("undo")
def cmd_undo(
    space: Annotated[str, typer.Option("--space")] = "company",
    receipt: Annotated[Path | None, typer.Option("--receipt", help="Defaults to the newest.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Reverse an apply, restoring every source from the quarantine."""
    from navig_cabinet.paperwork.undo import latest_receipt, undo as run_undo

    paths = _paths(space)
    target = receipt or latest_receipt(paths)
    if target is None:
        ch.error(f"no receipt found in {paths.receipts_dir} — nothing to undo")
        raise typer.Exit(2)
    if not target.exists():
        ch.error(f"receipt not found: {target}")
        raise typer.Exit(2)

    stats = run_undo(target, dry_run=dry_run)
    payload = {
        "dry_run": dry_run, "restored": stats.restored, "already_in_place": stats.already,
        "refused": stats.refused, "missing_from_quarantine": stats.missing,
        "errors": stats.errors, "receipt": str(target),
    }
    if as_json:
        ch.console.print_json(_json.dumps(payload, default=str))
    else:
        verb = "Would restore" if dry_run else "Restored"
        ch.success(f"{verb} {stats.restored} · already in place {stats.already} · "
                   f"missing from quarantine {stats.missing}")
        for err in stats.errors[:20]:
            ch.warning(err)

    if stats.failed:
        ch.error(f"{stats.refused} document(s) refused — they changed after filing and were kept.")
        raise typer.Exit(1)


@paperwork_app.command("handoff")
def cmd_handoff(
    space: Annotated[str, typer.Option("--space", help="Space holding the manifest.")] = "company",
    to_space: Annotated[str | None, typer.Option(
        "--to", help="Only this destination space — or `cabinet` for the ID/medical half.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Report what would move; touch nothing.")] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Required to actually move files.")] = False,
    no_ocr: Annotated[bool, typer.Option("--no-ocr", help="Do not read the text of documents going into the cabinet.")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """File the manifest's non-company documents where they belong.

    ID scans and medical records are encrypted into `navig cabinet` (originals left in
    place); benefits and housing letters are filed into the space that owns them.
    Separate from `apply` on purpose: scanning and filing must never be able to move a
    personal document as a side effect. This is the deliberate act.
    """
    from navig_cabinet.paperwork.handoff_apply import CABINET, apply_handoff, is_for_cabinet, read_manifest

    paths = _paths(space)
    entries = read_manifest(paths)
    if not entries:
        ch.info(f"No handoff manifest in {paths.base} — run `{PAPERWORK_CMD} scan` first.")
        return

    personal = [e for e in entries if is_for_cabinet(e)]
    routed = [e for e in entries if not is_for_cabinet(e) and e.get("suggested_space")]
    if to_space == CABINET:
        routed = []
    elif to_space:
        personal = []
        routed = [e for e in routed if e["suggested_space"] == to_space]
    if not routed and not personal:
        ch.warning(f"{len(entries)} manifest entry(ies), none with a destination here.")
        return

    if not (yes or dry_run):
        by_space: dict[str, int] = {}
        for e in routed:
            by_space[e["suggested_space"]] = by_space.get(e["suggested_space"], 0) + 1
        if routed:
            ch.info(f"{len(routed)} document(s) would be filed: {by_space}")
        if personal:
            ch.info(f"{len(personal)} ID/medical document(s) would be encrypted into the cabinet "
                    "(originals left where they are).")
        ch.dim("Nothing was changed. Re-run with --yes to do it, or --dry-run to rehearse.")
        return

    cabinet = None
    if personal and not dry_run:
        from navig_cabinet.commands.cabinet import open_cabinet

        cabinet = open_cabinet(create=True)
    elif personal:
        from navig_cabinet.store import Cabinet, default_root

        if Cabinet.exists(default_root()):
            from navig_cabinet.commands.cabinet import open_cabinet

            cabinet = open_cabinet()
    try:
        st = apply_handoff(paths, dry_run=dry_run, only_space=to_space, cabinet=cabinet,
                           read_text=not no_ocr)
    finally:
        if cabinet is not None:
            cabinet.close()
    payload = {
        "dry_run": dry_run, "moved": st.moved, "already_filed": st.skipped,
        "to_cabinet": st.to_cabinet, "already_in_cabinet": st.already_in_cabinet,
        "kept_for_a_tool": st.kept_for_tool, "no_destination": st.no_destination,
        "per_space": st.per_space, "errors": st.errors,
    }
    if as_json:
        ch.console.print_json(_json.dumps(payload, default=str))
    else:
        verb = "Would file" if dry_run else "Filed"
        if routed:
            ch.success(f"{verb} {st.moved} document(s) — {st.per_space or 'none'}")
        if personal:
            enc = "Would encrypt" if dry_run else "Encrypted"
            ch.success(f"{enc} {st.to_cabinet} ID/medical document(s) into the cabinet"
                       + (f" · {st.already_in_cabinet} already there" if st.already_in_cabinet else ""))
            if st.to_cabinet and not dry_run:
                ch.dim("The originals were left where they were. Check them with "
                       f"`{CMD} list --tag paperwork`, then delete the plain copies yourself.")
        if st.skipped:
            ch.dim(f"{st.skipped} already in place or no longer there")
        if st.kept_for_tool:
            ch.info(f"{st.kept_for_tool} credential(s) left where they are — import with "
                    "`navig vault`, never as plaintext files.")
        if st.no_destination:
            ch.info(f"{st.no_destination} document(s) belong to another person — not moved.")
        for message in st.errors[:20]:
            ch.warning(message)

    if st.failed:
        ch.error(f"{len(st.errors)} problem(s) — the affected sources were kept.")
        raise typer.Exit(1)


@paperwork_app.command("echeances")
def cmd_echeances(
    space: Annotated[str, typer.Option("--space")] = "company",
    horizon: Annotated[int | None, typer.Option("--horizon", help="Days ahead. Default: the space's renewal_alert_days (30).")] = None,
    send: Annotated[bool, typer.Option("--send", help="Send the radar to the operator on Telegram.")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """The deadline radar: what is overdue or due soon, from filed letters and recurrent renewals.

    Reads `mailroom/echeances.jsonl` (written when letters are filed) and
    `mailroom/recurrent.yaml` (hand-maintained renewals: CNI, passeport, assurance…);
    writes `mailroom/reports/echeancier.md`.
    """
    from navig_cabinet.paperwork import mailroom
    from navig_cabinet.paperwork.profiles import renewal_alert_days

    paths = _paths(space)
    days = horizon if horizon is not None else renewal_alert_days(paths.space_root)
    if days <= 0:
        ch.error("--horizon must be a positive number of days")
        raise typer.Exit(2)

    items = mailroom.radar(paths, horizon_days=days)
    report = mailroom.write_echeancier(paths, items, horizon_days=days)

    if as_json:
        ch.console.print_json(_json.dumps(
            {"horizon_days": days, "report": str(report), "count": len(items),
             "items": [{"due": d.due.isoformat(), "days": d.days, "status": d.status,
                        "organisme": d.organisme, "objet": d.objet, "action": d.action,
                        "source": d.source, "recurrent": d.recurrent} for d in items]},
            default=str,
        ))
    elif not items:
        ch.info(f"Aucune échéance dans les {days} prochains jours. Rapport → {report}")
    else:
        table = Table(title=f"{len(items)} échéance(s) — horizon {days} j")
        for col in ("Statut", "Échéance", "J", "Organisme", "Objet", "Action"):
            table.add_column(col, overflow="fold")
        style = {mailroom.EN_RETARD: "[red]", mailroom.URGENT: "[yellow]", mailroom.A_VENIR: "[green]"}
        for d in items:
            tag = style.get(d.status, "")
            table.add_row(f"{tag}{d.status}{'[/]' if tag else ''}", d.due.isoformat(), str(d.days),
                          d.organisme or "—", d.objet[:70], d.action)
        ch.console.print(table)
        ch.dim(f"Rapport → {report}")

    if send:
        from navig_cabinet._core import NEWER_CORE, notify_available, notify_operator

        if not notify_available():
            ch.warning(f"Telegram: not sent — sending needs {NEWER_CORE}.")
            raise typer.Exit(1)
        if notify_operator(mailroom.telegram_radar_text(items, horizon_days=days)):
            ch.dim("Telegram: sent.")
        else:
            ch.warning("Telegram: not sent (no chat configured or the Bot API refused).")
            raise typer.Exit(1)


@paperwork_app.command("reply")
def cmd_reply(
    document: Annotated[str, typer.Argument(help="The filed letter: path (absolute or relative to the space root).")],
    say: Annotated[str, typer.Option("--say", help="What you want to answer, in your own words.")],
    space: Annotated[str, typer.Option("--space")] = "company-paperwork",
    style: Annotated[Path | None, typer.Option("--style", help="Style prompt (default: docs/prompts/courrier-redaction.md).")] = None,
    model: Annotated[str | None, typer.Option("--model", help="provider:model, e.g. ollama:llama3")] = None,
    allow_cloud: Annotated[bool, typer.Option("--allow-cloud", help="Permit a non-local provider.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show the facts and the provider; write nothing.")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Draft the reply to a filed letter (French, formal) into out/courriers/ — never sent.

    The one model call in this plugin, through the guarded door: opt-in, the provider is
    printed, a non-local provider is refused without --allow-cloud.
    """
    from navig_cabinet._core import NEWER_CORE, llm_guard

    guard = llm_guard()
    if guard is None:
        ch.error(f"drafting replies needs {NEWER_CORE} — the guarded model door is not in this core.")
        raise typer.Exit(1)
    from navig_cabinet.paperwork import reply as R

    paths = _paths(space)
    try:
        d = R.draft_reply(paths.space_root, document, instruction=say, style_path=style,
                          model=model, allow_cloud=allow_cloud, dry_run=dry_run)
    except guard.CloudRefused as exc:
        ch.error(str(exc))
        raise typer.Exit(1) from exc
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        ch.error(str(exc))
        raise typer.Exit(2 if isinstance(exc, (FileNotFoundError, ValueError)) else 1) from exc
    payload = {"dry_run": dry_run, "draft": str(d.path), "provider": d.provider, "emetteur": d.emetteur,
               "objet": d.objet, "reference_found": bool(d.reference), "echeance": d.echeance, "action": d.action}
    if as_json:
        ch.console.print_json(_json.dumps(payload, default=str))
        return
    ch.info(f"Modèle : {d.provider}")
    ch.kv("Émetteur", d.emetteur or "—")
    ch.kv("Objet", d.objet or "—")
    ch.kv("Référence", "trouvée" if d.reference else "absente")
    ch.kv("Échéance", d.echeance or "—")
    if dry_run:
        ch.success(f"Would write {d.path}")
    else:
        ch.success(f"Brouillon → {d.path}   (relire, puis imprimer / envoyer soi-même)")


@paperwork_app.command("index")
def cmd_index(
    space: Annotated[str, typer.Option("--space")] = "company",
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Rebuild the document index and the invoice register from disk."""
    from navig_cabinet.paperwork import ledger, register

    paths = _paths(space)
    docs = ledger.build(paths)
    if not docs:
        # Not a failure: an unfiled space is simply empty.
        ch.info(f"No filed documents yet under {paths.space_root}.")
        return

    summary = ledger.write(paths, docs)
    entries = register.build(paths, docs)
    totals = register.write(paths, entries) if entries else None

    if as_json:
        payload = dict(summary)
        if totals is not None:
            payload["register"] = {
                "invoices": totals.count, "invoiced_eur": round(totals.invoiced, 2),
                "paid_eur": round(totals.paid, 2), "unpaid_eur": round(totals.unpaid, 2),
                "status_unrecorded_eur": round(totals.unknown, 2),
            }
        ch.console.print_json(_json.dumps(payload, default=str))
        return

    ch.success(f"Indexed {summary['count']} document(s) → {paths.index_md}")
    ch.kv("Next invoice number", summary["next_invoice_id"])
    if totals is not None:
        ch.kv("Invoices in register", totals.count)
        ch.kv("Invoiced", f"{totals.invoiced:,.2f} EUR")
        # Paid is the number that matters: a micro-entrepreneur declares encaissement,
        # so billing an invoice is not the taxable event — being paid for it is.
        ch.kv("Paid (declarable)", f"{totals.paid:,.2f} EUR")
        if totals.unpaid:
            ch.kv("Unpaid", f"{totals.unpaid:,.2f} EUR")
        if totals.unknown:
            ch.warning(
                f"{totals.unknown:,.2f} EUR has no payment status recorded — set it in "
                "finance/invoices/register.csv (your edits survive a rebuild)."
            )
    if summary["series_gaps"]:
        rendered = ", ".join(f"INV-{n:06d}" for n in summary["series_gaps"])
        ch.warning(
            f"Gap in the issued series: {rendered} missing. French invoicing requires a "
            "continuous, gapless sequence."
        )
