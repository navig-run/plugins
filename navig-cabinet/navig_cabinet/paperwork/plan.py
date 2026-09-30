"""The plan: one reviewable CSV row per source file, and the scan that produces it.

The plan **is** the review gate. ``scan`` reads the source drives and writes this file;
``apply`` reads only this file and touches nothing else. Between them a person can open
it in Excel, correct a ``decision`` or a ``dest_rel``, and re-run — which is a better
review surface for 377 rows than any interactive prompt loop.

Written as ``utf-8-sig`` so Excel renders ``é`` instead of ``Ã©`` (the same choice
``navig_explore.route`` makes for its manifest).
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
from dataclasses import asdict, dataclass, fields as dc_fields
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Sequence

from . import dedupe, naming
from .classify import classify, decision_for
from .extract import Extracted, extract_text, sha256_file

# Directories that never hold company paperwork. Skipped whole, so a 136 MB wordlist
# and a 149 MB ebook library never reach the extractor.
SKIP_DIRS = frozenset(
    {"ebooks", "guides-reference", "bookmarks-web", "node_modules", ".git",
     "__pycache__", "sante-mutuelle", "petit panflets", "presentations"}
)
SKIP_SUFFIXES = frozenset({".exe", ".dll", ".msi", ".iso", ".tar", ".gz", ".zip", ".7z"})
MAX_BYTES = 64 * 1024 * 1024
# Below this much extracted text a letter is filed only after a person looks at it.
MIN_LETTER_CHARS = 120

# Paperwork only, by default. Pointing this at a downloads folder otherwise sweeps in
# whatever else lives there — one real run over `C:\Users\<me>\Downloads` produced a
# 31,512-row plan of which 20,571 rows were `.jpg` from a messaging app's media cache
# and 5,471 were `.mp3`. None of it could ever be an invoice, all of it had to be paid
# for in OCR time, and it buried the 800 documents that mattered.
#
# `--all-types` lifts this for the rare archive that keeps paperwork as images.
PAPER_SUFFIXES = frozenset(
    {".pdf", ".docx", ".doc", ".odt", ".rtf", ".xlsx", ".xls", ".ods", ".csv",
     ".txt", ".md", ".html", ".htm", ".eml", ".msg", ".pptx", ".ppt"}
)


@dataclass
class PlanRow:
    row_id: str = ""
    decision: str = "review"
    doc_class: str = "unknown"
    confidence: float = 0.0
    dest_rel: str = ""
    src: str = ""
    size: int = 0
    sha256: str = ""
    dup_group: str = ""
    dup_role: str = ""
    near_group: str = ""
    doc_date: str = ""
    doc_id: str = ""
    counterparty: str = ""
    expense_category: str = ""
    amount: str = ""
    signals: str = ""
    extract: str = ""
    original_name: str = ""
    veto_subclass: str = ""
    mtime: float = 0.0
    notes: str = ""
    # Personal profile — the dossier facts of a letter. Empty under `business`.
    profile: str = ""
    emetteur: str = ""
    echeance: str = ""
    reference: str = ""
    action_requise: str = ""
    channel: str = ""
    objet: str = ""


COLUMNS: tuple[str, ...] = tuple(f.name for f in dc_fields(PlanRow))


def row_id_for(src: str | Path) -> str:
    """Stable across rescans, unique per source path — the key ``apply`` uses."""
    return hashlib.sha1(str(Path(src).resolve()).lower().encode("utf-8")).hexdigest()[:12]


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ── Walking ─────────────────────────────────────────────────────────────────


def iter_candidates(roots: Sequence[Path], *, all_types: bool = False) -> Iterable[Path]:
    """Every file worth considering under *roots*, skipping known-irrelevant trees."""
    seen: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        if root.is_file():
            yield root
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS]
            for name in filenames:
                p = Path(dirpath) / name
                suffix = p.suffix.lower()
                if suffix in SKIP_SUFFIXES:
                    continue
                if not all_types and suffix not in PAPER_SUFFIXES:
                    continue
                key = str(p.resolve()).lower()
                if key in seen:
                    continue
                seen.add(key)
                yield p


# ── Scanning ────────────────────────────────────────────────────────────────


def scan(
    roots: Sequence[Path],
    *,
    ocr: bool = True,
    cloud: bool = False,
    max_pages: int = 3,
    all_types: bool = False,
    limit: int | None = None,
    on_progress: Callable[[int, Path], None] | None = None,
    evidence_out: Path | None = None,
    profile: str = "business",
    channels: list[tuple[str, list[str]]] | None = None,
) -> list[PlanRow]:
    """Extract, classify and deduplicate every candidate. Writes nothing but evidence.

    ``profile`` is ``business`` (invoices/quotes/tax) or ``personal`` (the person's
    administrative mail, see ``profiles.py``); ``channels`` is the space's routes.yaml
    taxonomy for the personal profile.
    """
    rows: list[PlanRow] = []
    evidence: list[dict] = []
    # Under the personal profile the whole point is reading photographed letters, so
    # image files are candidates by default; the business default stays paper-only
    # (see PAPER_SUFFIXES for why).
    if profile == "personal":
        all_types = True

    for n, path in enumerate(iter_candidates(roots, all_types=all_types), 1):
        if limit and len(rows) >= limit:
            break
        try:
            st = path.stat()
        except OSError:
            continue
        if st.st_size > MAX_BYTES or st.st_size == 0:
            continue

        if on_progress:
            on_progress(n, path)

        ex: Extracted = extract_text(path, ocr=ocr, cloud=cloud, max_pages=max_pages)
        pc = classify(ex.text, path.name, profile=profile, channels=channels)
        digest = sha256_file(path)
        decision = decision_for(pc)
        note = ""
        if profile == "personal" and decision == "migrate" and len(ex.text.strip()) < MIN_LETTER_CHARS:
            # A photographed letter that OCR could barely read is not evidence for
            # a confident filing — a person checks it first. The photo stays intact.
            decision = "review"
            note = "texte OCR trop court — verifier"

        row = PlanRow(
            row_id=row_id_for(path),
            decision=decision,
            doc_class=pc.doc_class,
            confidence=pc.confidence,
            src=str(path),
            size=st.st_size,
            sha256=digest,
            doc_date=pc.doc_date,
            doc_id=pc.doc_id,
            counterparty=pc.counterparty,
            expense_category=pc.expense_category,
            amount=pc.amount,
            signals="+".join(pc.signals),
            extract=ex.method,
            original_name=path.name,
            veto_subclass=pc.veto_subclass or "",
            mtime=st.st_mtime,
            notes=note,
            profile=profile,
            emetteur=pc.emetteur,
            echeance=pc.echeance,
            reference=pc.reference,
            action_requise=pc.action_requise,
            channel=pc.channel,
            objet=pc.objet,
        )
        rows.append(row)
        evidence.append(
            {
                "row_id": row.row_id,
                "src": row.src,
                "extract": ex.method,
                "pages": ex.pages,
                "errors": list(ex.errors),
                "text_len": len(ex.text),
                "signals": pc.signals,
            }
        )

    _mark_duplicates(rows, [str(r) for r in roots])
    _assign_destinations(rows)

    if evidence_out is not None:
        evidence_out.parent.mkdir(parents=True, exist_ok=True)
        with evidence_out.open("w", encoding="utf-8") as f:
            for item in evidence:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")

    return rows


def _mark_duplicates(rows: list[PlanRow], source_order: Sequence[str]) -> None:
    for digest, members in dedupe.exact_groups(rows).items():
        keeper = dedupe.elect_keeper(members, source_order)
        for m in members:
            m.dup_group = digest[:12]
            if m is keeper:
                m.dup_role = "keep"
            else:
                m.dup_role = "dupe"
                # A handoff row is never migrated anyway; leave its decision alone so
                # the manifest still reports every copy of a personal document.
                if m.decision != "handoff":
                    m.decision = "trash-dupe"

    for group, members in dedupe.near_groups(rows).items():
        reason = dedupe.describe_difference(members)
        for m in members:
            if m.dup_role == "dupe":
                continue  # already resolved as an exact copy
            m.near_group = group
            if m.decision == "migrate":
                m.decision = "review"
            m.notes = (m.notes + "; " if m.notes else "") + reason

    # Never quarantine the copies of a document that is not itself being filed. The
    # near-duplicate pass above can demote a keeper to `review` after its exact twins
    # were already marked `trash-dupe` — which left INV-000024-25 with three copies
    # bound for the quarantine and no copy bound for the archive. Nothing was lost
    # (the keeper stays where it is), but the archive silently gains a hole.
    keepers: dict[str, PlanRow] = {r.dup_group: r for r in rows if r.dup_role == "keep"}
    for r in rows:
        if r.decision != "trash-dupe":
            continue
        kept = keepers.get(r.dup_group)
        if kept is not None and kept.decision != "migrate":
            r.decision = "review"
            r.notes = (r.notes + "; " if r.notes else "") + "held: kept copy needs review"


def _assign_destinations(rows: list[PlanRow]) -> None:
    """Give every migratable row a destination, and guarantee handoff rows have none."""
    taken: dict[str, str] = {}
    for r in rows:
        if r.doc_class in naming.NO_DESTINATION or r.decision == "handoff":
            r.dest_rel = ""
            continue
        if r.decision == "trash-dupe":
            r.dest_rel = ""
            continue
        dest = naming.destination(r)
        if dest in taken and taken[dest] != r.sha256:
            # Two different documents want the same name. Disambiguate rather than
            # letting `apply` discover the collision file-by-file. `.as_posix()` is
            # required: `str(PurePath)` renders backslashes on Windows, and dest_rel is
            # a posix path everywhere else in the plan.
            p = PurePosixPath(dest)
            dest = p.with_name(f"{p.stem}-{r.sha256[:6]}{p.suffix}").as_posix()
            r.notes = (r.notes + "; " if r.notes else "") + "name disambiguated"
        taken[dest] = r.sha256
        r.dest_rel = dest


# ── CSV ─────────────────────────────────────────────────────────────────────


def write_csv(rows: Sequence[PlanRow], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(COLUMNS))
        w.writeheader()
        for r in rows:
            w.writerow(asdict(r))


def read_csv(path: Path) -> list[PlanRow]:
    """Read a plan back, tolerating hand-edits. Unknown columns are ignored."""
    out: list[PlanRow] = []
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        for raw in csv.DictReader(f):
            row = PlanRow()
            for name in COLUMNS:
                if name not in raw or raw[name] is None:
                    continue
                value = raw[name]
                current = getattr(row, name)
                try:
                    if isinstance(current, float):
                        setattr(row, name, float(value or 0))
                    elif isinstance(current, int):
                        setattr(row, name, int(float(value or 0)))
                    else:
                        setattr(row, name, value)
                except (TypeError, ValueError):
                    pass  # a hand-edited cell that will not parse keeps its default
            out.append(row)
    return out


def summarize(rows: Sequence[PlanRow]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.decision] = counts.get(r.decision, 0) + 1
    return dict(sorted(counts.items()))
