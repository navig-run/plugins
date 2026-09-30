"""Collapse exact duplicates automatically; never collapse near-duplicates.

The split matters more than it looks. This corpus has 47 exact-duplicate groups —
one URSSAF attestation stored under six different *dated* filenames, five copies of
``INV-000022-25`` straddling two folders — and clearing those needs no human.

But it also has families that look identical and are not: ``Facture F5520198601 (2)``
is a *DUPLICATA 26.10.20* while ``(3)`` is *22.07.20*, and ``20230326220945.pdf`` /
``_compressed.pdf`` / ``-1-8.pdf`` are a page-subset family. Auto-merging those loses
a document. So near-duplicates are grouped, flagged, and handed to a person.

There is deliberately no perceptual or fuzzy-text matching. At this corpus size no
similarity threshold is defensible, and the failure mode — silently discarding a real
document — is the one that destroys trust in the tool.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Iterable, Protocol, Sequence, TypeVar

from . import signals as S
from .names import normalized_stem

class DedupeRow(Protocol):
    """The four fields these helpers actually read.

    Structural rather than nominal so the grouping logic can be exercised with a
    plain stub, and so nothing here can quietly start depending on the rest of
    ``PlanRow``.
    """

    src: str
    sha256: str
    size: int
    mtime: float


Row = TypeVar("Row", bound=DedupeRow)


def exact_groups(rows: Sequence[Row]) -> dict[str, list[Row]]:
    """Group rows by content hash, keeping only groups with more than one member."""
    by_hash: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        if r.sha256:
            by_hash[r.sha256].append(r)
    return {h: rs for h, rs in by_hash.items() if len(rs) > 1}


def elect_keeper(rows: Sequence[Row], source_order: Sequence[str]) -> Row:
    """Pick the canonical copy of an exact-duplicate group, deterministically.

    Determinism is a requirement, not a nicety: a rescan must reproduce the same
    answer, or a second ``apply`` would migrate a different member and leave two.
    """
    order = {str(Path(s).resolve()).lower(): i for i, s in enumerate(source_order)}

    def source_rank(row) -> int:
        p = str(Path(row.src).resolve()).lower()
        for root, idx in order.items():
            if p.startswith(root):
                return idx
        return len(order)

    def canonical_stem(row) -> int:
        # An exactly-canonical name beats a decorated one: `INV-000022-25.pdf`
        # outranks `INV-000022-25 (1).pdf`.
        return 0 if S.INV_SERIES_STEM.match(Path(row.src).stem) else 1

    return min(
        rows,
        key=lambda r: (
            source_rank(r),
            canonical_stem(r),
            len(Path(r.src).stem),
            r.mtime,
            str(r.src).lower(),
        ),
    )


def near_groups(rows: Iterable[Row]) -> dict[str, list[Row]]:
    """Group by normalized stem; keep groups holding more than one distinct hash."""
    by_stem: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        key = normalized_stem(Path(r.src).stem)
        if key:
            by_stem[key].append(r)
    out: dict[str, list[Row]] = {}
    n = 0
    for key, members in sorted(by_stem.items()):
        if len({m.sha256 for m in members if m.sha256}) > 1:
            n += 1
            out[f"near-{n:04d}"] = members
    return out


def describe_difference(members: Sequence[Row]) -> str:
    """A one-line reason a human can act on, shown in the plan's ``notes``."""
    sizes = sorted({m.size for m in members})
    if len(sizes) > 1:
        rendered = " vs ".join(f"{s / 1024:.0f}KB" for s in sizes)
        return f"same name, different content — sizes: {rendered}"
    return "same name and size, different content"
