"""Putting files into the cabinet — the one path every ``add``/``import`` goes through."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .categories import kind_of, suggest_category
from .extract import TextResult, extract_text
from .store import Cabinet, CabinetError, Duplicate, Item, sha256_file


@dataclass
class Added:
    item: Item
    text: TextResult


def add_path(cabinet: Cabinet, path: Path, *, title: str | None = None,
             category: str | None = None, tags: list[str] | None = None,
             expires: str | None = None, issuer: str | None = None, notes: str | None = None,
             read_text: bool = True, transcribe: bool = False,
             allow_duplicate: bool = False) -> Added:
    """Encrypt one file into the cabinet. The source file is never modified.

    The checksum is taken first so a duplicate is refused before a multi-gigabyte
    video is encrypted a second time; ``add_stream`` then re-checks the bytes it
    actually read against it, so a file that changes mid-add is refused, not stored.
    """
    if not path.is_file():
        raise CabinetError(f"{path}: not a file")
    sha = sha256_file(path)
    if not allow_duplicate:
        existing = cabinet.find_by_sha(sha)
        if existing is not None:
            raise Duplicate(existing)
    kind, mime = kind_of(path.name)
    text = extract_text(path, kind, transcribe=transcribe) if read_text else TextResult()
    detected = False
    if expires is None and text.text:
        # A date the document states about itself (MRZ check-digit verified, or a
        # labelled "date d'expiration"), so reminders work without any typing.
        from .dates import find_expiry

        found = find_expiry(text.text)
        if found:
            expires, detected = found.expires, True
    with path.open("rb") as src:
        item = cabinet.add_stream(
            src, original_name=path.name, kind=kind, mime=mime,
            category=category or suggest_category(path.name, kind, text.text),
            title=title, tags=tags, expires=expires, issuer=issuer, notes=notes,
            text=text.text, text_source=text.source, expected_sha256=sha,
            allow_duplicate=allow_duplicate, expires_detected=detected,
        )
    return Added(item, text)


def expand(paths: list[Path], *, recursive: bool = True) -> list[Path]:
    """Files named directly, plus every file under a named folder (hidden ones skipped)."""
    out: list[Path] = []
    for p in paths:
        if p.is_dir():
            walker = p.rglob("*") if recursive else p.glob("*")
            out.extend(sorted(f for f in walker if f.is_file()
                              and not any(part.startswith(".") for part in f.relative_to(p).parts)))
        else:
            out.append(p)
    return out


# ── navig paperwork handoff ────────────────────────────────────────────────────

# The paperwork plugin's veto subclasses that are a person's own records. Benefits and
# housing letters are paperwork a space works with (deadlines, replies); these are
# documents you keep. Third-party documents are never anyone's to import.
PAPERWORK_CATEGORIES = {"identity": "identity", "health": "medical", "health-domain": "medical"}


@dataclass
class ImportRow:
    src: str
    outcome: str            # imported | already | missing | changed | skipped | error
    detail: str = ""
    item_id: str | None = None


@dataclass
class ImportReport:
    rows: list[ImportRow] = field(default_factory=list)

    def count(self, outcome: str) -> int:
        return sum(1 for r in self.rows if r.outcome == outcome)


def read_manifest(path: Path) -> list[dict]:
    entries = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise CabinetError(f"{path}:{n}: not valid JSON ({exc.msg})") from exc
    return entries


def import_paperwork(cabinet: Cabinet | None, manifest: Path, *, read_text: bool = True,
                     dry_run: bool = False) -> ImportReport:
    """Encrypt the personal documents listed in a paperwork handoff manifest."""
    return import_entries(cabinet, read_manifest(manifest), read_text=read_text, dry_run=dry_run)


def import_entries(cabinet: Cabinet | None, entries: list[dict], *, read_text: bool = True,
                   dry_run: bool = False) -> ImportReport:
    """Encrypt the personal documents a paperwork scan set aside. Originals stay put.

    Each file must still hash to what the scan recorded: a file that changed since
    the scan is not the document the scan classified, so it is reported, not stored.
    ``cabinet`` may be ``None`` only for a dry run before any cabinet exists — there is
    nothing to deduplicate against yet.
    """
    if cabinet is None and not dry_run:
        raise ValueError("a real import needs a cabinet")
    report = ImportReport()
    for e in entries:
        src = str(e.get("src") or "")
        category = PAPERWORK_CATEGORIES.get(e.get("subclass") or "")
        if category is None:
            report.rows.append(ImportRow(src, "skipped", f"not personal ({e.get('subclass') or 'unknown'})"))
            continue
        p = Path(src)
        if not p.is_file():
            report.rows.append(ImportRow(src, "missing", "file is no longer there"))
            continue
        expected = e.get("sha256")
        actual = sha256_file(p)
        if expected and actual != expected:
            report.rows.append(ImportRow(src, "changed", "file changed since the paperwork scan"))
            continue
        existing = cabinet.find_by_sha(actual) if cabinet is not None else None
        if existing is not None:
            report.rows.append(ImportRow(src, "already", existing.title, existing.id))
            continue
        if dry_run:
            report.rows.append(ImportRow(src, "imported", f"would import as {category}"))
            continue
        assert cabinet is not None
        try:
            added = add_path(cabinet, p, category=category, tags=["paperwork"],
                             title=e.get("original_name") and Path(e["original_name"]).stem,
                             read_text=read_text)
            report.rows.append(ImportRow(src, "imported", category, added.item.id))
        except (CabinetError, OSError) as exc:
            report.rows.append(ImportRow(src, "error", str(exc)))
    return report
