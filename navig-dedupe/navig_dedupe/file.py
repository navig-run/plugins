"""Exact-duplicate detection via content SHA-256 (any file type).

Pure engine — the ``navig-dedupe`` / ``navig dedupe`` CLI drives it. Non-destructive.

Hashing every file to find the duplicates reads the entire tree, which is the wrong
price for the answer: **a file whose size is unique cannot have a byte-identical twin**,
so most of a library can be excluded without opening it. Two cheap sieves run first:

1. **size** (``os.stat``, no read at all) — keeps only files that share a byte count;
2. **head** (first 64 KB) — keeps only those that also start identically.

Only what survives both is hashed in full. The sieves are exact, not heuristic: identical
files always share a size and a head, so nothing is ever missed. On a real 29,055-file /
193 GB music library this took the full-hash pass from 193 GB to 2.1 GB — 93× less read —
and on a library with no duplicates at all it approaches zero.
"""
from __future__ import annotations

import hashlib
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HEAD_BYTES = 64 << 10


def sha256(path: Path, buf: int = 1 << 20) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while chunk := f.read(buf):
                h.update(chunk)
        return h.hexdigest()
    except Exception:  # noqa: BLE001
        return None


def head_sha256(path: Path, n: int = HEAD_BYTES) -> str | None:
    """SHA-256 of the first ``n`` bytes. For a file at or under ``n`` bytes this is the
    hash of the whole file, so the caller can reuse it instead of reading twice."""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read(n)).hexdigest()
    except Exception:  # noqa: BLE001
        return None


# Folders that exist to hold copies already set aside — by this tool (`--move` writes
# into one) or by a previous pass. Descending into them makes every quarantined file
# report as a duplicate of the original it was quarantined *for*, which reads as "these
# are redundant, quarantine them" about files that already are. Skipped by default when
# they appear INSIDE the scanned tree; pointing the root directly at one still scans it.
DEFAULT_SKIP_DIRS = frozenset({
    ".trash", "_trash", "trash", ".recycle", "$recycle.bin", "recycler",
    "_dupes", "_duplicates", "_quarantine", "quarantine",
    "system volume information", "__macosx",
    # `_organized` holds hard-linked VIEWS of the library (see
    # navig_explore.vision.arrange) — the same file under a second name. Scanning
    # it reports every photo as a duplicate of itself, which is both wrong and
    # exactly the kind of "obvious" finding someone acts on.
    "_organized",
})


def _sizes(root: Path, recursive: bool,
           skip: frozenset[str] | set[str] | None = None) -> list[tuple[Path, int]]:
    skip = {s.lower() for s in (skip if skip is not None else DEFAULT_SKIP_DIRS)}
    out: list[tuple[Path, int]] = []
    if not recursive:
        try:
            entries = list(root.iterdir())
        except OSError:
            return out
        for p in entries:
            try:
                if p.is_file():
                    out.append((p, p.stat().st_size))
            except OSError:
                continue
        return out
    # os.walk (not rglob) so a skipped directory is never descended into at all —
    # on a 4 TB drive the difference is minutes of pointless stat calls.
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None,
                                                followlinks=False):
        dirnames[:] = [d for d in dirnames if d.lower() not in skip]
        for fn in filenames:
            p = Path(dirpath) / fn
            try:
                out.append((p, p.stat().st_size))
            except OSError:  # vanished or unreadable between listing and stat
                continue
    return out


def hash_dir(root: Path, recursive: bool = False, workers: int = 8,
             progress=None, skip: frozenset[str] | set[str] | None = None) -> dict[str, str]:
    """``{rel_path: sha256}`` for every file that could be part of a duplicate group.

    Files ruled out by size or head are omitted rather than hashed — they cannot cluster
    with anything, so :func:`cluster` produces exactly the same groups as hashing the
    whole tree would. ``progress(done, total)`` is reported against the surviving
    candidate set, which is what the run actually has to read.

    ``skip`` is a set of directory NAMES not to descend into (recursive scans only);
    it defaults to :data:`DEFAULT_SKIP_DIRS`. Pass ``skip=set()`` to scan everything.
    """
    sized = _sizes(root, recursive, skip)

    by_size: dict[int, list[Path]] = defaultdict(list)
    for p, s in sized:
        by_size[s].append(p)
    # sieve 1 — a unique byte count cannot collide with anything
    candidates = [(p, s) for s, ps in by_size.items() if len(ps) > 1 for p in ps]

    # sieve 2 — same size, different first 64 KB
    heads: dict[Path, str] = {}
    with ThreadPoolExecutor(workers) as ex:
        for (p, _s), h in zip(candidates, ex.map(head_sha256, (p for p, _ in candidates))):
            if h is not None:
                heads[p] = h
    by_head: dict[tuple[int, str], list[Path]] = defaultdict(list)
    for p, s in candidates:
        if p in heads:
            by_head[(s, heads[p])].append(p)

    survivors = [(p, s) for (s, _h), ps in by_head.items() if len(ps) > 1 for p in ps]

    out: dict[str, str] = {}
    done = 0
    total = len(survivors)
    with ThreadPoolExecutor(workers) as ex:
        # a file no larger than the head IS its head — reuse it rather than re-read
        small = [(p, s) for p, s in survivors if s <= HEAD_BYTES]
        large = [(p, s) for p, s in survivors if s > HEAD_BYTES]
        for p, _s in small:
            out[str(p.relative_to(root))] = heads[p]
            done += 1
        for (p, _s), h in zip(large, ex.map(sha256, (p for p, _ in large))):
            done += 1
            if h:
                out[str(p.relative_to(root))] = h
            if progress and done % 1000 == 0:
                progress(done, total)
    if progress and total:
        progress(done, total)
    return out


def cluster(hashes: dict[str, str]) -> list[list[str]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for rel, h in hashes.items():
        groups[h].append(rel)
    return [sorted(g) for g in groups.values() if len(g) > 1]
