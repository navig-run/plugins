"""
The vCard import pipeline, end to end.

    parse -> normalise -> cluster on hard identifiers -> classify by tier
          -> drop the archive tier (reporting it) -> persist -> report

Kept separate from the Click command so the whole thing can be run and
asserted on in tests without a CLI runner, and so it can move into a navig
plugin later without dragging Click along.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .interests import ArchiveSplit, collect_given_names, split_archive
from .merge import Cluster, build_clusters, persist
from .photos import PhotoResult, extract_photos
from .models import ImportSummary
from .vcard import iter_vcf_files, parse_target
from .vcf_reports import write_all


@dataclass
class VcfImportResult:
    summary: ImportSummary
    clusters: list[Cluster] = field(default_factory=list)
    importable: list[Cluster] = field(default_factory=list)
    split: Optional[ArchiveSplit] = None
    rejected: list = field(default_factory=list)
    assumed: list = field(default_factory=list)
    cards: int = 0
    files: list[str] = field(default_factory=list)
    reports: dict[str, Path] = field(default_factory=dict)
    outcomes: list[dict] = field(default_factory=list)
    photos: Optional[PhotoResult] = None

    def tier_counts(self) -> dict[str, int]:
        counts = {"verified": 0, "email": 0, "handle": 0, "archive": 0}
        for cluster in self.clusters:
            counts[cluster.tier()] += 1
        return counts


def analyse(target: Path) -> VcfImportResult:
    """Parse and cluster a .vcf file or directory without touching the DB."""
    cards = parse_target(target)
    result = build_clusters(cards)
    importable = [c for c in result.clusters if c.tier() != "archive"]
    archive = [c for c in result.clusters if c.tier() == "archive"]

    # Given names are harvested from the cards that carry a surname, so a bare
    # "Olga" in the archive tier can be recognised as a person rather than a
    # brand — evidence from this archive, not a bundled name dictionary.
    given = collect_given_names(cards)

    return VcfImportResult(
        summary=ImportSummary(source="vcf"),
        clusters=result.clusters,
        importable=importable,
        split=split_archive(archive, given),
        rejected=result.rejected,
        assumed=result.assumed,
        cards=len(cards),
        files=[p.name for p in iter_vcf_files(target)],
    )


def import_vcf(target: Path, conn, session_id: str,
               source_label: str = "vcf-archive",
               photos_dir: Optional[Path] = None) -> VcfImportResult:
    """
    Import one .vcf file or directory into an open connection.

    Archive-tier clusters are deliberately not passed to :func:`persist` — they
    have no phone, no email and no handle, and the operator's instruction was to
    keep the non-people as an interests list and drop the rest.
    """
    result = analyse(target)
    outcome = persist(conn, result.importable, session_id,
                      source_label, result.summary)
    result.outcomes = outcome["outcomes"]

    # Skype embedded the avatar in the card; for most of these people it is
    # the only picture that survives, so pull it out and file it.
    if photos_dir is not None:
        result.photos = extract_photos(result.importable, photos_dir)
        result.summary.photos_matched = result.photos.extracted
        for alias, rel in result.photos.paths.items():
            conn.execute(
                "UPDATE contacts SET photo_path = ? WHERE alias = ? "
                "AND (photo_path IS NULL OR photo_path = '')",
                (rel, alias),
            )
    return result


def reviewed_aliases(conn) -> set:
    """Contacts whose flagged merge has already been judged correct."""
    return {r[0] for r in conn.execute(
        "SELECT alias FROM contacts WHERE merge_reviewed_at IS NOT NULL")}


def write_reports(result: VcfImportResult, exports_dir: Path,
                  reviewed: Optional[set] = None) -> dict[str, Path]:
    """Write every report for a completed (or dry-run) analysis."""
    result.reports = write_all(
        exports_dir, result.clusters, result.rejected,
        result.assumed, result.split, reviewed,
    )
    return result.reports
