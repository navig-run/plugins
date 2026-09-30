"""Execute a handoff manifest: file non-company documents into the spaces that own them.

This is the one place in the plugin that writes outside the space it was pointed at,
and it is deliberately a **separate, explicitly-invoked command** rather than part of
``apply``. The scan-and-file path must never be able to move a personal document into
another space as a side effect — that is the property the whole veto design exists to
guarantee. Here the operator has read the manifest and asked for it by name.

Three rules keep it honest:

* **Only what the manifest already decided.** Destinations come from the manifest's own
  ``suggested_space``; this command never re-classifies and never invents a target.
* **A tool is not a space.** Entries routed to ``navig vault`` (live credentials) are
  reported and left alone — writing a recovery code into a markdown tree is the wrong
  answer regardless of which tree.
* **A person's own documents are encrypted, not filed.** ID scans and medical records go
  into the cabinet (``navig cabinet``, same plugin) by their *type*, even when an older
  manifest still names a space for them — a manifest written before this rule must not
  put a passport scan back into a plaintext tree. Their originals are left in place.
* **Nobody else's documents.** Entries with no suggested space — a third party's
  paperwork — are never moved.

Files land in the destination space's own convention: ``records/<YYYY-MM>_<slug>/``,
which is the shape ``human-health-space`` already uses for scanned records.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..ingest import PAPERWORK_CATEGORIES, ImportReport, import_entries
from .apply import _copy_verify, _sha256, _to_trash, _write_receipt
from .names import repair, slug
from .plan import PlanRow
from .space import PaperworkPaths, resolve_space_root

# One dated folder per migration batch, mirroring human-health-space's own
# `records/2024-07_tdm-rachis-lombaire/` shape.
BATCH = "archive-h-docs"


@dataclass
class HandoffStats:
    moved: int = 0
    skipped: int = 0
    kept_for_tool: int = 0
    no_destination: int = 0
    mismatch: int = 0
    errors: list[str] = field(default_factory=list)
    per_space: dict[str, int] = field(default_factory=dict)
    # The encrypted-cabinet half: ID and medical documents.
    to_cabinet: int = 0
    already_in_cabinet: int = 0
    awaiting_cabinet: int = 0          # found, but no cabinet was provided to put them in
    cabinet_report: ImportReport | None = None

    @property
    def failed(self) -> bool:
        return bool(self.mismatch or self.errors)


def read_manifest(paths: PaperworkPaths) -> list[dict]:
    if not paths.handoff_jsonl.exists():
        return []
    out = []
    for line in paths.handoff_jsonl.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def destination_for(entry: dict, space_root: Path, *, batch_date: str) -> Path:
    """Where this document lands inside its destination space."""
    subclass = entry.get("subclass") or "unsorted"
    name = entry.get("original_name") or Path(entry["src"]).name
    stem, suffix = Path(name).stem, Path(name).suffix
    safe = slug(repair(stem), max_len=90) or (entry.get("sha256", "") or "doc")[:8]
    return space_root / "records" / f"{batch_date}_{BATCH}" / subclass / f"{safe}{suffix}"


CABINET = "cabinet"  # the --to value that selects the encrypted half


def is_for_cabinet(entry: dict) -> bool:
    return (entry.get("subclass") or "") in PAPERWORK_CATEGORIES


def apply_handoff(
    paths: PaperworkPaths,
    *,
    dry_run: bool = False,
    only_space: str | None = None,
    cabinet=None,
    read_text: bool = True,
) -> HandoffStats:
    """File every manifest entry that has a destination. Idempotent.

    ``cabinet`` is an open :class:`navig_cabinet.store.Cabinet`. Without one, ID and
    medical entries are counted in ``awaiting_cabinet`` and left untouched — never
    filed into a space as a fallback.
    """
    st = HandoffStats()
    entries = read_manifest(paths)
    if not entries:
        return st

    personal = [e for e in entries if is_for_cabinet(e)]
    if personal and only_space in (None, CABINET):
        if cabinet is None and not dry_run:
            st.awaiting_cabinet = len(personal)
        else:
            report = import_entries(cabinet, personal, read_text=read_text, dry_run=dry_run)
            st.cabinet_report = report
            st.to_cabinet = report.count("imported")
            st.already_in_cabinet = report.count("already")
            st.skipped += report.count("missing")
            for row in report.rows:
                if row.outcome in {"changed", "error"}:
                    st.errors.append(f"{row.src}: {row.detail}")
    entries = [e for e in entries if not is_for_cabinet(e)]
    if only_space == CABINET:
        return st

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    batch_date = datetime.now(timezone.utc).strftime("%Y-%m")
    receipt = paths.receipt(f"{stamp}-handoff") if not dry_run else None
    if receipt is not None:
        receipt.parent.mkdir(parents=True, exist_ok=True)

    roots: dict[str, Path] = {}

    for entry in entries:
        space = entry.get("suggested_space")
        if not space:
            if entry.get("suggested_tool"):
                st.kept_for_tool += 1
            else:
                st.no_destination += 1
            continue
        if only_space and space != only_space:
            continue

        if space not in roots:
            try:
                roots[space] = resolve_space_root(space)
            except ValueError as exc:
                st.errors.append(str(exc))
                roots[space] = None  # type: ignore[assignment]
        root = roots[space]
        if root is None:
            continue

        src = Path(entry["src"])
        dest = destination_for(entry, root, batch_date=batch_date)

        if dest.exists() and _sha256(dest) == entry.get("sha256"):
            st.skipped += 1                      # already filed — rerun is free
            continue
        if dest.exists():
            # A DIFFERENT document already owns this name. Two source files whose
            # names slug identically — `avis_de_situation.pdf` and
            # `avis de situation (1).pdf` — collide here, and overwriting silently
            # loses one of them. Give this one its own name instead.
            digest8 = (entry.get("sha256") or _sha256(src))[:8] or "dup"
            dest = dest.with_name(f"{dest.stem}-{digest8}{dest.suffix}")
            if dest.exists() and _sha256(dest) == entry.get("sha256"):
                st.skipped += 1
                continue
        if not src.exists():
            st.skipped += 1
            continue
        if dry_run:
            st.moved += 1
            st.per_space[space] = st.per_space.get(space, 0) + 1
            continue

        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".copytmp")
        digest = _copy_verify(src, tmp)
        if not digest:
            st.mismatch += 1
            st.errors.append(f"copy verification failed for {src}")
            continue
        tmp.replace(dest)

        # Quarantine inside the DESTINATION space, so that space owns both the
        # document and its recovery path.
        try:
            trash = str(_to_trash(src, PaperworkPaths(root).trash_root))
        except OSError as exc:
            st.errors.append(f"{src}: filed but source not quarantined: {exc}")
            trash = ""

        st.moved += 1
        st.per_space[space] = st.per_space.get(space, 0) + 1

        _write_receipt(
            receipt,
            PlanRow(
                row_id=entry.get("row_id", ""),
                src=entry["src"],
                sha256=entry.get("sha256", ""),
                doc_class=f"handoff:{entry.get('subclass')}",
            ),
            action="handed-off", dest=str(dest), trash=trash, sha256=digest,
        )

    return st
