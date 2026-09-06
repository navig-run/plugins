"""Browsable folders per person, year, place or kind — without moving anything.

A photo of three people belongs in three "person" folders, and a photo has a
year *and* a place *and* the people in it. Moving files can only ever express one
of those, so this builds **hardlinks** instead: one file on disk, many directory
entries, no extra space and the original never leaves its curated location.

That matters here specifically. This library's ~300 human-named event folders
carry meaning no metadata can rebuild, so the answer to "show me folders per
person" must not be "dismantle the event folders". Delete an arranged tree
whenever you like — the originals are untouched by construction.

Falls back to copying only when a hardlink is impossible (a different volume),
and says so rather than silently doubling the disk usage.
"""
from __future__ import annotations

import csv
import os
import re
import shutil
from pathlib import Path

FACETS = ("person", "year", "place", "class", "month", "event", "screenshot-type",
          "screenshot-source", "flat", "document-type", "subject")

#: `flat` puts every matching file directly in the destination with no bucket
#: subfolder — "one folder of all the webcam frames", not one folder per year.
FLAT = "flat"

# Below this a date is a hint, not grounds to file a photo under a year.
MIN_DATE_CONFIDENCE = 0.80

# A group whose faces all come from a handful of source images is a repeated
# frame or a piece of artwork, not a person. Requiring several DISTINCT photos is
# what removes the folders that turned out to hold a mask and a cartoon emoji.
MIN_DISTINCT_PHOTOS = 10

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# `By Date/2009/2009-02` is a date bucket; `By Date/2009/Alzon 2009` is an event.
_DATE_BUCKET = re.compile(r"^\d{4}-\d{2}$")


def _safe(name: str) -> str:
    """A folder name Windows will accept, without losing the label's meaning."""
    out = _UNSAFE.sub("-", str(name)).strip(" .")
    return out or "_unknown"


def _bucket_dir(dest: Path, bucket: str) -> Path:
    """Bucket to folder, letting a label like ``nature/sunset`` nest.

    Each component is sanitised separately, so a slash in the taxonomy makes a
    subfolder while a slash inside a person's name still cannot escape `dest`.
    """
    out = dest
    for part in str(bucket).split("/"):
        part = part.strip()
        if part:
            out = out / _safe(part)
    return out if out != dest else dest / "_unknown"


#: How sure a sub-type must be before it names a folder; below it, `other`.
#: Taken against the runner-up, for the reason given in `classify.confident` —
#: this bar has to survive the taxonomy growing, and a full-softmax one does not.
#:
#: At 0.60: screenshots leave 23.9% in `other` (16 types) and documents 18.0%
#: (6 types) — two taxonomies of very different size landing in the same place,
#: which is the whole point of measuring against the runner-up.
SUBTYPE_MIN_PROBABILITY = 0.60


def _subtypes(conn, root: Path, cls_sql: str, cls_args: tuple, which: str,
              ) -> dict[str, tuple[str, list[str]]]:
    """Split an already-known class into sub-types — documents, screenshots.

    Uses the SigLIP embeddings already in the catalog, so this costs a matrix
    product rather than another pass over the images.
    """
    from . import catalog, classify, embed as E  # noqa: PLC0415

    rows = list(conn.execute(f"""
        SELECT f.path, f.name, f.sha256 FROM files f
        WHERE f.present = 1 AND f.root = ?
          {cls_sql}""", (str(root), *cls_args)))
    if not rows:
        return {}

    keys, mat = catalog.load_matrix(conn, E.model_id())
    idx = {k: i for i, k in enumerate(keys)}
    types, protos = ((None, None) if not keys else
                     (classify.doc_prototypes() if which == "document"
                      else classify.shot_prototypes()))

    out: dict[str, tuple[str, list[str]]] = {}
    for r in rows:
        i = idx.get(r["sha256"])
        if i is None:
            # No embedding — undecodable, or indexed before this pass existed.
            # It still belongs to the class, so it gets the honest bucket rather
            # than vanishing from a view that claims to hold everything of its kind.
            label = "other"
        else:
            # Same rule as everywhere else here: an unclear call gets an honest
            # bucket rather than a confident-looking wrong one.
            pick, _p = classify.confident(protos @ mat[i], bar=SUBTYPE_MIN_PROBABILITY)
            label = types[pick] if pick is not None else "other"
        name = r["name"] or Path(r["path"]).name
        out.setdefault(r["path"], (name, []))[1].append(label)
    return out


def _class_clause(only_classes: tuple[str, ...] | None) -> tuple[str, tuple]:
    """SQL fragment restricting a view to given content classes."""
    if not only_classes:
        return "", ()
    marks = ",".join("?" * len(only_classes))
    return (f" AND EXISTS (SELECT 1 FROM classes c2 WHERE c2.sha256 = f.sha256 "
            f"AND c2.class IN ({marks}))", tuple(only_classes))


def buckets_for(conn, root: Path, facet: str, *,
                min_confidence: float = MIN_DATE_CONFIDENCE,
                include_unnamed: bool = False, min_group: int = 1,
                min_distinct_photos: int = 1,
                only_classes: tuple[str, ...] | None = None
                ) -> dict[str, tuple[str, list[str]]]:
    """→ ``{absolute path: (original filename, [bucket, ...])}``.

    The filename comes from ``files.name``, not from the path: paths are stored
    normcased, so deriving the name from one lowercases it and the view ends up
    full of ``img_1.jpg`` where the library has ``IMG_1.jpg``.
    """
    root = Path(root).resolve()
    out: dict[str, tuple[str, list[str]]] = {}
    cls_sql, cls_args = _class_clause(only_classes)

    if facet == "person":
        # Unnamed groups are still people — they just have no label yet. Excluding
        # them made a person view of 2,373 photos out of 22,613 that are grouped.
        # With include_unnamed they appear as `person-<id>`, which is exactly the
        # handle `photos people name` takes.
        rows = conn.execute(f"""
            SELECT f.path, f.name,
                   COALESCE(p.name, 'person-' || p.person_id) AS bucket
            FROM files f
            JOIN faces fa ON fa.sha256 = f.sha256
            JOIN people p ON p.person_id = fa.person_id
            WHERE f.present = 1 AND f.root = ?
              {'' if include_unnamed else 'AND p.name IS NOT NULL'}
              AND (SELECT COUNT(*) FROM faces x
                   WHERE x.person_id = p.person_id) >= ?
              AND (SELECT COUNT(DISTINCT x.sha256) FROM faces x
                   WHERE x.person_id = p.person_id) >= ?
              {cls_sql}""",
            (str(root), min_group, min_distinct_photos, *cls_args))
    elif facet in ("year", "month"):
        span = 4 if facet == "year" else 7
        rows = conn.execute(f"""
            SELECT f.path, f.name, substr(d.value, 1, {span}) AS bucket
            FROM files f
            JOIN dates d ON d.sha256 = f.sha256
            WHERE f.present = 1 AND f.root = ? AND d.confidence >= ?
              AND d.value IS NOT NULL AND d.value <> ''
              {cls_sql}""", (str(root), min_confidence, *cls_args))
    elif facet == "place":
        rows = conn.execute(f"""
            SELECT f.path, f.name,
                   COALESCE(g.place, '') ||
                   CASE WHEN g.country IS NOT NULL AND g.country <> ''
                        THEN ', ' || g.country ELSE '' END AS bucket
            FROM files f
            JOIN geo g ON g.sha256 = f.sha256
            WHERE f.present = 1 AND f.root = ? AND g.place IS NOT NULL
              {cls_sql}""", (str(root), *cls_args))
    elif facet == "class":
        rows = conn.execute("""
            SELECT f.path, f.name, c.class AS bucket
            FROM files f
            JOIN classes c ON c.sha256 = f.sha256
            WHERE f.present = 1 AND f.root = ?""", (str(root),))
    elif facet == "event":
        # Reads the `events` table rather than the path, so an occasion the
        # operator never named is as reachable as one they did. Bucket is
        # `<year>/<name>`, which _bucket_dir nests into two folders.
        #
        # A CURATED event bypasses the class filter: its contents are whatever
        # the human put there, and 40 such events vanished entirely when webcam
        # frames and salvage were filtered out. Undecodable bytes are still
        # skipped — there is nothing to look at.
        if only_classes:
            marks = ",".join("?" * len(only_classes))
            keep = (f"AND (e.source = 'folder' OR EXISTS ("
                    f"SELECT 1 FROM classes c2 WHERE c2.sha256 = f.sha256 "
                    f"AND c2.class IN ({marks})))")
            args: tuple = (str(root), *only_classes)
        else:
            keep, args = "", (str(root),)
        rows = conn.execute(f"""
            SELECT f.path, f.name, e.year || '/' || e.event_name AS bucket
            FROM files f
            JOIN events e ON e.sha256 = f.sha256
            JOIN assets a ON a.sha256 = f.sha256 AND a.decoded = 1
            WHERE f.present = 1 AND f.root = ?
              {keep}""", args)
    elif facet == "subject":
        # Multi-label by design: a photo of dinner at sunset belongs in both.
        rows = conn.execute(f"""
            SELECT f.path, f.name, s.grp || '/' || s.subject AS bucket
            FROM files f
            JOIN subjects s ON s.sha256 = f.sha256
            WHERE f.present = 1 AND f.root = ?
              {cls_sql}""", (str(root), *cls_args))
    elif facet == FLAT:
        rows = conn.execute(f"""
            SELECT f.path, f.name, '{FLAT}' AS bucket FROM files f
            WHERE f.present = 1 AND f.root = ?
              {cls_sql}""", (str(root), *cls_args))
    elif facet == "document-type":
        # Sub-typing only makes sense for things already known to be documents,
        # so the caller passes only_classes=("document",).
        return _subtypes(conn, root, cls_sql, cls_args, "document")
    elif facet == "screenshot-type":
        # What the capture is OF, which is what anyone actually looks for.
        return _subtypes(conn, root, cls_sql, cls_args, "screenshot")
    elif facet == "screenshot-source":
        # Which DEVICE took a screen capture. Kept because the answer is exact —
        # a screenshot has no camera EXIF by definition, and the screen
        # resolution settles it: 1170x2532 IS an iPhone 13/14 Pro. It is no
        # longer the default view: 11,469 of 15,130 captures here came from one
        # phone, so splitting by device produced one enormous folder called
        # "iPhone" and nothing inside it was findable.
        from .classify_rules import device_for  # noqa: PLC0415

        for r in conn.execute(f"""
                SELECT f.path, f.name, f.p_camera AS cam, a.w, a.h
                FROM files f JOIN assets a ON a.sha256 = f.sha256
                WHERE f.present = 1 AND f.root = ?
                  {cls_sql}""", (str(root), *cls_args)):
            name = r["name"] or Path(r["path"]).name
            out.setdefault(r["path"], (name, []))[1].append(
                device_for(r["w"] or 0, r["h"] or 0, camera=r["cam"] or ""))
        return out
    else:
        raise ValueError(f"unknown facet {facet!r}; pick one of {', '.join(FACETS)}")

    for r in rows:
        b = (r["bucket"] or "").strip()
        if not b:
            continue
        name = r["name"] or Path(r["path"]).name
        out.setdefault(r["path"], (name, []))[1].append(b)
    return out


class DestinationInsideLibrary(ValueError):
    """Raised when the arranged tree would land inside the indexed library.

    Not a style objection — a hardlink farm inside the library is picked up by
    the next `probe`/`index` run as thousands of new files, double-counting the
    whole library, and `dedup` then sees every link as a duplicate of its own
    original. The tree belongs beside the library, not in it.
    """


# Directory names every scanner in this repo walks past. A views tree under one
# of these is inside the library *and* invisible to indexing — which is what the
# operator actually wants: the folders next to their photos, not on a sibling drive.
SAFE_INSIDE = frozenset({"_organized", ".trash", "_trash", ".mediaexplorer"})


def _is_safely_inside(root: Path, dest: Path) -> bool:
    try:
        rel = dest.relative_to(root)
    except ValueError:
        return False
    return any(p.lower() in SAFE_INSIDE for p in rel.parts)


def build_plan(root: Path, dest: Path, facet: str, *,
               min_confidence: float = MIN_DATE_CONFIDENCE,
               include_unnamed: bool = False, min_group: int = 1,
               min_distinct_photos: int = 1,
               only_classes: tuple[str, ...] | None = None,
               limit: int | None = None, force: bool = False) -> tuple[list[dict], dict]:
    """Compute the link plan. Pure — touches nothing on disk."""
    from . import catalog  # noqa: PLC0415

    root = Path(root).resolve()
    dest = Path(dest).expanduser().resolve()
    inside = dest == root or root in dest.parents
    if inside and not force and not _is_safely_inside(root, dest):
        raise DestinationInsideLibrary(
            f"{dest} is inside the library at {root}.\n"
            f"The next `probe`/`index` would count every link as a new photo, and "
            f"`dedup` would treat it as a duplicate of its own original.\n"
            f"Use a folder the scanners skip — {root / '_organized'} — or put it "
            f"alongside, e.g. {root.parent / ('_views-' + facet)}")
    conn = catalog.connect(root)

    mapping = buckets_for(conn, root, facet, min_confidence=min_confidence,
                          include_unnamed=include_unnamed, min_group=min_group,
                          min_distinct_photos=min_distinct_photos,
                          only_classes=only_classes)
    rows: list[dict] = []
    summary: dict[str, int] = {}
    for src, (name, bucket_list) in sorted(mapping.items()):
        for b in sorted(set(bucket_list)):
            # A flat view is one folder of everything — no per-bucket subfolder.
            dst = ((dest / _safe(name)) if facet == FLAT
                   else (_bucket_dir(dest, b) / _safe(name)))
            rows.append({"bucket": b, "src": src, "dst": str(dst)})
            summary[b] = summary.get(b, 0) + 1
        if limit and len(rows) >= limit:
            break
    return rows, summary


def write_plan(rows: list[dict], out: Path) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["bucket", "src", "dst"])
        w.writeheader()
        w.writerows(rows)
    return out


def _unique(dst: Path, src: Path | None = None) -> Path:
    """A free name beside `dst` — or the existing one that is already `src`.

    Reusing the existing entry is what makes a re-run idempotent. Without it the
    second run makes `X__1.jpg`, the third `X__2.jpg`, and the view grows one
    copy per run for every colliding filename. That was invisible for as long as
    the whole tree was deleted before each rebuild, and became load-bearing the
    moment the rebuild started reusing it.
    """
    if not dst.exists():
        return dst
    stem, suffix, i = dst.stem, dst.suffix, 1
    while True:
        cand = dst.with_name(f"{stem}__{i}{suffix}")
        if not cand.exists():
            return cand
        try:
            if src is not None and os.path.samefile(src, cand):
                return cand
        except OSError:
            pass
        i += 1


def apply_plan(rows: list[dict], log_path: Path, *, allow_copy: bool = True,
               dest: Path | None = None, prune: bool = False) -> dict:
    """Create the links. Originals are never read, moved or modified.

    With ``dest`` and ``prune``, entries under ``dest`` that the plan no longer
    wants are removed afterwards, making the tree match the plan rather than
    accumulate. That is the difference between a rebuild and a **sync**: the old
    way was to delete all 147k links and remake them, ~8 minutes of unlinking
    plus ~4 of linking on a USB drive, to change one folder's name.

    Creating comes first and deleting second on purpose — a photograph that
    merely moved bucket exists at its new name before the old one goes, so the
    tree is never missing anything a reader might be looking at.
    """
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stats = {"linked": 0, "copied": 0, "existed": 0, "failed": 0, "missing": 0}
    wanted: set[str] = set()

    with log_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["bucket", "src", "dst", "how"])
        for r in rows:
            src, dst = Path(r["src"]), Path(r["dst"])
            if not src.exists():
                stats["missing"] += 1
                continue
            if dst.exists():
                # Two cases that must NOT be conflated. If this entry already
                # points at the same file, re-running is a no-op — that is what
                # makes `arrange` idempotent. If it is a DIFFERENT photo that
                # merely shares a filename (IMG_0001.JPG from two event folders),
                # skipping silently drops it from the view: 27 photos went
                # missing this way before the distinction existed.
                try:
                    if os.path.samefile(src, dst):
                        stats["existed"] += 1
                        wanted.add(os.path.normcase(str(dst)))
                        w.writerow([r["bucket"], str(src), str(dst), "existed"])
                        continue
                except OSError:
                    pass
                dst = _unique(dst, src)
                if dst.exists():                    # reused: already this photo
                    stats["existed"] += 1
                    wanted.add(os.path.normcase(str(dst)))
                    w.writerow([r["bucket"], str(src), str(dst), "existed"])
                    continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            how = "link"
            try:
                os.link(src, dst)
            except OSError:
                # A different volume, or a filesystem without hard links.
                if not allow_copy:
                    stats["failed"] += 1
                    continue
                try:
                    shutil.copy2(src, dst)
                    how = "copy"
                except OSError:
                    stats["failed"] += 1
                    continue
            stats["linked" if how == "link" else "copied"] += 1
            wanted.add(os.path.normcase(str(dst)))
            w.writerow([r["bucket"], str(src), str(dst), how])

    if prune and dest is not None:
        stats.update(prune_to(Path(dest), wanted))
    return stats


#: Files the view tree writes about itself; never candidates for pruning.
KEEP_IN_DEST = frozenset({"_read-me-first.md", "_lost-occasions.txt"})


def prune_to(dest: Path, wanted: set[str]) -> dict:
    """Remove entries under `dest` the plan no longer wants, then empty folders.

    `wanted` holds normcased absolute paths. Anything else under `dest` is a
    link from a previous shape of the tree — a renamed event, a reclassified
    screenshot — and removing it is what stops the two shapes accumulating.
    Only ever unlinks; the original always has at least its own name.
    """
    stats = {"pruned": 0, "prune_failed": 0, "dirs_removed": 0}
    if not dest.exists():
        return stats
    for path in list(dest.rglob("*")):
        if not path.is_file():
            continue
        if path.parent == dest and path.name.lower() in KEEP_IN_DEST:
            continue
        if os.path.normcase(str(path)) in wanted:
            continue
        try:
            path.unlink()
            stats["pruned"] += 1
        except OSError:
            stats["prune_failed"] += 1
    # Deepest first, so a folder emptied by its children's removal also goes.
    for d in sorted((p for p in dest.rglob("*") if p.is_dir()),
                    key=lambda p: -len(p.parts)):
        try:
            d.rmdir()
            stats["dirs_removed"] += 1
        except OSError:
            pass
    return stats


def _why_undeletable(p: Path) -> str:
    """The one explanation worth naming; everything else falls back to the OSError."""
    import stat as _stat  # noqa: PLC0415

    try:
        attrs = getattr(p.stat(), "st_file_attributes", 0)
    except OSError:
        return ""
    ro = getattr(_stat, "FILE_ATTRIBUTE_READONLY", 0x1)
    return "read-only (the attribute is on the original, shared by every link)" \
        if attrs & ro else ""


def remove(log_path: Path) -> dict:
    """Delete an arranged tree, link by link.

    Safe by construction for the linked entries: removing one directory entry of
    a hard-linked file leaves the original untouched. Copies (the cross-volume
    fallback) are the only rows where this reclaims real bytes.

    A link that will not delete is reported rather than merely counted. The one
    encountered here was read-only — an attribute NTFS stores once per file and
    every hard link therefore shares, so the flag is really on the original in
    `Albums/Products`. Clearing it to tidy a view would quietly modify a
    photograph the operator marked, so the link is left in place instead and
    named in `failures`.
    """
    stats: dict = {"unlinked": 0, "copies_removed": 0, "missing": 0, "failed": 0,
                   "failures": []}
    with Path(log_path).open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    dirs: set[Path] = set()
    for r in rows:
        p = Path(r["dst"])
        if not p.exists():
            stats["missing"] += 1
            continue
        try:
            p.unlink()
        except OSError as exc:
            stats["failed"] += 1
            if len(stats["failures"]) < 20:
                stats["failures"].append(f"{p}: {_why_undeletable(p) or exc}")
            continue
        stats["copies_removed" if r.get("how") == "copy" else "unlinked"] += 1
        dirs.add(p.parent)
    for d in sorted(dirs, key=lambda x: -len(x.parts)):
        try:
            d.rmdir()
        except OSError:
            pass
    return stats
