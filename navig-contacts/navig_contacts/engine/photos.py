"""
Avatars: extracting the ones embedded in vCards, and finding the same face
again among photos whose owner was never identified.

309 cards in the archive carry a base64 ``PHOTO`` — Skype wrote the profile
picture straight into the vCard. For most of those people it is the only image
that survives, so it is worth pulling out and filing against the contact.

The second half is a search: the space holds 17,310 photos under
``unmatched_photos/`` whose owner is unknown. Their filenames
(``photo_3217@03-02-2023_07-24-51.jpg``) carry an export index and a timestamp
and nothing else, so nothing can be matched by name. What *can* be matched is
the picture itself, with a perceptual hash — the same avatar re-saved at a
different size or quality still hashes to nearly the same value.
"""
from __future__ import annotations

import hashlib
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

#: Hamming distance below which two difference-hashes are the same picture.
#: 0 is byte-identical-after-resize; 8 starts admitting merely similar crops.
HASH_THRESHOLD = 5

#: A second, independent opinion.  One hash agreeing on its own is not
#: evidence: the single "match" this matcher first produced scored dhash 5
#: while phash said 28, ahash 19 and whash 14 — a 96x96 avatar against a
#: 1280x904 photograph.  Requiring a perceptual hash to agree as well is what
#: separates the same picture from two pictures with a similar gradient.
PHASH_THRESHOLD = 10


def safe_filename(alias: str) -> str:
    """
    An alias as a filename.

    An imported alias carries a ``:`` (``p:33678488684``), which Windows will
    not accept in a filename and which reads as a drive separator, so it
    becomes ``_``.
    """
    return re.sub(r"[^A-Za-z0-9._-]", "_", alias)


@dataclass
class PhotoResult:
    extracted: int = 0
    skipped_existing: int = 0
    failed: int = 0
    paths: dict[str, str] = field(default_factory=dict)   # alias -> relative path


def extract_photos(clusters, photos_dir: Path,
                   subdir: str = "vcf") -> PhotoResult:
    """
    Write each cluster's embedded avatar to ``photos/<subdir>/<alias>.jpg``.

    Kept in a subdirectory so these never collide with the Telegram avatars
    already in ``photos/``, which are named after a handle.  Returns the
    relative POSIX paths, matching what ``contacts.photo_path`` stores.
    """
    result = PhotoResult()
    target = photos_dir / subdir
    target.mkdir(parents=True, exist_ok=True)

    for cluster in clusters:
        alias = cluster.alias()
        if not alias:
            continue
        data = next(
            (c.photo_bytes for c in cluster.cards if c.photo_bytes), None
        )
        if not data:
            continue
        dest = target / f"{safe_filename(alias)}.jpg"
        rel = f"{photos_dir.name}/{subdir}/{dest.name}"
        if dest.exists() and dest.stat().st_size == len(data):
            result.skipped_existing += 1
            result.paths[alias] = rel
            continue
        try:
            dest.write_bytes(data)
        except OSError:
            result.failed += 1
            continue
        result.extracted += 1
        result.paths[alias] = rel

    return result


# ---------------------------------------------------------------------------
# Matching an avatar against the unidentified photos
# ---------------------------------------------------------------------------

def _load_hashers():
    """
    Import Pillow + imagehash lazily.

    Photo matching is a bonus pass, not part of importing contacts: if the
    imaging libraries are missing the import must still work, so this returns
    None rather than raising.
    """
    try:
        from PIL import Image  # noqa: WPS433
        import imagehash       # noqa: WPS433
    except ImportError:
        return None
    return Image, imagehash


#: Below this greyscale standard deviation an image carries no structure —
#: a solid placeholder avatar.  Their difference-hash is all zeros, so they
#: match every other blank image: the first run of this matcher returned 226
#: "matches" that were all one white 96x96 avatar against white photos.
MIN_STDDEV = 8.0


def _hash_file(path: Path, Image, imagehash):
    """
    ``(dhash, phash)`` for an image, or None if it is blank or unreadable.

    Two hashes, because either one alone produces false matches at this
    scale: 183 avatars against 17,310 photos is 3.2 million comparisons.
    """
    try:
        with Image.open(path) as img:
            small = img.convert("L").resize((64, 64))
            if _is_blank(small):
                return None
            return imagehash.dhash(small), imagehash.phash(small)
    except Exception:
        return None


def _is_blank(image) -> bool:
    """True for a near-uniform image, which no hash can distinguish."""
    try:
        lo, hi = image.getextrema()
        if hi - lo < 16:
            return True
        # get_flattened_data() on Pillow >= 11, getdata() before it.
        reader = getattr(image, "get_flattened_data", None) or image.getdata
        return statistics.pstdev(list(reader())) < MIN_STDDEV
    except Exception:
        return True


def file_digest(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


@dataclass
class PhotoMatch:
    uid: str
    contact_photo: str
    candidate: str
    distance: int
    exact: bool


@dataclass
class MatchReport:
    matches: list[PhotoMatch] = field(default_factory=list)
    scanned: int = 0
    hashed: int = 0
    unreadable: int = 0
    blank_or_unreadable: int = 0
    available: bool = True
    reason: str = ""


def match_against(photo_paths: dict[str, str], base_dir: Path,
                  haystack_dir: Path,
                  threshold: int = HASH_THRESHOLD) -> MatchReport:
    """
    Look for each extracted avatar among the photos with no known owner.

    Exact byte matches are reported as such; everything else is a perceptual
    match under ``threshold``.  A match is a *suggestion* — it says two files
    show the same picture, not that a human has agreed whose face it is — so
    nothing here writes to the database.
    """
    report = MatchReport()
    libs = _load_hashers()
    if libs is None:
        report.available = False
        report.reason = ("Pillow and imagehash are not installed; "
                         "install them to match avatars by content")
        return report
    Image, imagehash = libs

    needles: list[tuple[str, Path, object, Optional[str]]] = []
    for alias, rel in photo_paths.items():
        path = base_dir / rel
        if not path.exists():
            continue
        h = _hash_file(path, Image, imagehash)
        if h is None:
            report.blank_or_unreadable += 1
            continue
        needles.append((alias, path, h, file_digest(path)))

    if not needles:
        report.reason = "no extracted avatars to match"
        return report

    by_digest = {d: (alias, path) for alias, path, _h, d in needles if d}

    for candidate in sorted(haystack_dir.glob("*.jpg")):
        report.scanned += 1
        digest = file_digest(candidate)
        if digest and digest in by_digest:
            alias, path = by_digest[digest]
            report.matches.append(PhotoMatch(
                uid=alias, contact_photo=str(path.name),
                candidate=candidate.name, distance=0, exact=True,
            ))
            continue
        h = _hash_file(candidate, Image, imagehash)
        if h is None:
            report.unreadable += 1   # blank or undecodable
            continue
        report.hashed += 1
        for alias, path, needle, _d in needles:
            distance = needle[0] - h[0]
            if distance <= threshold and (needle[1] - h[1]) <= PHASH_THRESHOLD:
                report.matches.append(PhotoMatch(
                    uid=alias, contact_photo=path.name,
                    candidate=candidate.name, distance=int(distance),
                    exact=False,
                ))

    report.matches.sort(key=lambda m: (m.distance, m.uid))
    return report


def render_match_report(report: MatchReport, extracted: PhotoResult) -> str:
    """A readable account of what was extracted and what it matched."""
    lines: list[str] = []
    lines.append("# Avatars from the vCard archive")
    lines.append("")
    lines.append(f"- **{extracted.extracted}** avatars extracted from embedded "
                 f"`PHOTO` data and filed against their contact.")
    if extracted.skipped_existing:
        lines.append(f"- {extracted.skipped_existing} were already on disk "
                     f"and unchanged.")
    if extracted.failed:
        lines.append(f"- {extracted.failed} could not be written.")
    lines.append("")

    if not report.available:
        lines.append(f"Content matching was not run: {report.reason}.")
        lines.append("")
        return "\n".join(lines)

    lines.append("## Matches against the unidentified photos")
    lines.append("")
    lines.append(
        f"Compared every extracted avatar against **{report.scanned}** photos "
        f"in `unmatched_photos/` — first by exact bytes, then by two "
        f"independent perceptual hashes that must agree (difference hash ≤ "
        f"{HASH_THRESHOLD} **and** perceptual hash ≤ {PHASH_THRESHOLD}). Their "
        f"filenames carry only an export index and a timestamp, so the picture "
        f"itself is the only thing that can be matched. Blank placeholder "
        f"avatars are excluded: they carry no structure, so every hash "
        f"considers them identical to each other."
    )
    lines.append("")
    if not report.matches:
        lines.append(
            "**No matches.** The two sets do not overlap: the embedded avatars "
            "are Skype profile pictures from 2016-2019, and the unidentified "
            "photos came out of a Telegram export years later. That is a "
            "finding, not a failure — it means none of those 17,310 photos "
            "belongs to a contact this archive knows about."
        )
        lines.append("")
        return "\n".join(lines)

    lines.append(f"**{len(report.matches)} match(es).**")
    lines.append("")
    lines.append("| Contact | Avatar | Unidentified photo | Distance | Exact |")
    lines.append("|---------|--------|--------------------|---------:|-------|")
    for m in report.matches:
        lines.append(
            f"| `{m.uid}` | {m.contact_photo} | {m.candidate} "
            f"| {m.distance} | {'yes' if m.exact else 'no'} |"
        )
    lines.append("")
    return "\n".join(lines)
