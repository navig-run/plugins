"""Photo-library organiser for navig-explore.

Files a messy photo tree into ``<date-root>/<YYYY>/<YYYY-MM>/`` from EXIF capture
dates, quarantines the litter, and — the part that matters — REFUSES to touch the
organisation a human already did.

Consumes ``meta.jsonl`` (probe) and optionally ``dupes.jsonl`` (dedup). Emits a
manifest CSV; ``apply_plan`` executes it as same-volume renames with an undo log.

Why this exists as its own module rather than a flag on ``route``
-----------------------------------------------------------------
``route`` migrates media BETWEEN volumes by provenance, copy-verify-trash. This
reorganises a library IN PLACE, and in-place work on a personal photo archive has a
failure mode that copying does not: silently destroying meaning.

The four rules, learned the hard way on a 169k-file / 300 GB archive
--------------------------------------------------------------------
1. **Human-named folders are sacred.** A real library's date tree is not uniform —
   it contains ``2009/Alzon 2009``, ``2009/14 juillet 2009``, ``2014/Берлин 2014``.
   Flattening those into ``2009/2009-07`` destroys context that no metadata can
   rebuild. Only ``YYYY-MM`` buckets, ``YYYY-00`` stubs, ``dd.mm.yy`` day folders and
   explicitly-declared unsorted piles may be reshaped.
2. **Dates come from EXIF, never the filesystem clock.** Archives assembled from
   recovery tools (PhotoRec/Recuva) carry meaningless mtimes — that is exactly how
   ``1970`` and ``2031`` folders appear. No capture date => the file stays undated
   rather than filed under a lie.
3. **An empty ``.txt`` is a caption, not litter.** Some folders are described only by
   a zero-byte file whose *name* is the note ("a la plage.txt").
4. **Nothing is deleted.** Quarantine moves under ``<root>/.trash/<bucket>/`` keeping
   the original relative path, and every run writes an undo log as it goes.
"""
from __future__ import annotations

import csv
import json
import os
import re
import time
from collections import defaultdict
from pathlib import Path

# ── classification ──────────────────────────────────────────────────────────
JUNK_EXT = {".ithmb", ".db", ".ini", ".lnk", ".pnf", ".inf", ".cat", ".sys",
            ".cab", ".ex_", ".inx", ".iss", ".ctg", ".url", ".wab", ".info"}
JUNK_NAME = {"thumbs.db", "desktop.ini", "picasa.ini", ".picasa.ini",
             "zbthumbnail.info", "ehthumbs.db"}
#: folders that are unambiguously machine-generated cache.
#: Deliberately NOT matching "thumbnails"/"thumbs": a folder by that name is often a
#: category the operator made on purpose (a real library had ``Web/Thumbnails`` full
#: of wanted images), and there is no way to tell it from cache by name alone.
JUNK_DIR_RE = re.compile(r"(?i)(^|/)(ipod photo cache|\.picasaoriginals|"
                         r"picasafiles)(/|$)")
#: an empty file with one of these extensions is a caption, not a broken remnant
CAPTION_EXT = {".txt", ".md", ".nfo"}

VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".wmv", ".3gp", ".mpg",
             ".mpeg", ".webm", ".mts", ".m2ts", ".flv"}
VIDEO_SIDECAR_EXT = {".thm", ".lrv"}

#: recovery-tool output — these names carry no trustworthy date at all
RECOVERED_RE = re.compile(
    r"(?i)^(file_\d+|lostfile[_-]|recoverd|recovered|unknown_\d+|\[\d+\])")

YM_RE = re.compile(r"^(?:19|20)\d{2}-(?:0[1-9]|1[0-2])$")
Y00_RE = re.compile(r"^(?:19|20)\d{2}-00$")
DAY_RE = re.compile(r"^\d{2}[.\-_]\d{2}[.\-_]\d{2,4}$")
#: machine-generated bulk-export folder names — a name, but not a HUMAN one
DUMP_RE = re.compile(r"(?i)^(import|_?loose.*|canon|\d{3}[a-z]{4,7}|"
                     r"[a-z0-9]+_\d{8}(_part_\d+)?)$")

FNAME_DATE = [
    re.compile(r"(?i)(?:IMG|VID|PXL|MVIMG|PANO|BURST|SAVE)[-_]?(19|20)(\d{2})(\d{2})(\d{2})"),
    re.compile(r"(?i)(19|20)(\d{2})(\d{2})(\d{2})[-_]\d{6}"),
    re.compile(r"(?i)(19|20)(\d{2})[-_.](\d{2})[-_.](\d{2})"),
    re.compile(r"(?i)(19|20)(\d{2})_(\d{2})_(\d{2})_"),
]


def _year_re(date_root: str) -> re.Pattern:
    return re.compile(r"(?i)^" + re.escape(date_root.replace("\\", "/"))
                      + r"/((?:19|20)\d{2})/([^/]+)(/|$)")


def placement(rel: str, date_root: str = "By Date",
              max_year: int | None = None) -> str:
    """How a file is currently filed — decides whether re-dating may touch it.

    ``named``  inside a human-named event folder. NEVER reshaped: this is the most
               valuable organisation in the library and is unrecoverable once lost.
    ``day``    a ``dd.mm.yy`` folder — already finer-grained than YYYY-MM. Kept.
    ``bucket`` already in ``YYYY-MM``. Kept when the capture date agrees.
    ``stub``   a ``YYYY-00`` placeholder; EXIF may upgrade it to a real month.
    ``loose``  fair game for re-dating.
    """
    max_year = max_year or time.localtime().tm_year
    m = _year_re(date_root).match(rel.replace("\\", "/"))
    if not m:
        return "loose"
    # A year that cannot be real is not a placement worth protecting: a future year
    # or epoch-zero is the classic symptom of a broken timestamp, not of curation.
    year = int(m.group(1))
    if year > max_year or year <= 1970:
        return "loose"
    sub = m.group(2)
    if YM_RE.match(sub):
        return "bucket"
    if Y00_RE.match(sub):
        return "stub"
    if DAY_RE.match(sub):
        return "day"
    if DUMP_RE.match(sub):
        return "loose"
    return "named"


#: How much MEANING a location carries. When two files are the same image, the copy in
#: the higher-ranked place is the one worth keeping — because that placement is the part
#: that cannot be reconstructed.
#:
#: A date bucket can be rebuilt from EXIF at any time. "By Date/2009/Alzon 2009" cannot:
#: someone decided those photos belong to that event, and nothing in the file records it.
#: Ranking by file size instead — the natural fallback when equal pixels tie every other
#: term — silently prefers whichever copy happens to carry more EXIF, which on a real
#: 121k-photo library meant 10,713 photos would have been stripped OUT of named event and
#: day folders while the copies kept sat in date buckets that EXIF can rebuild.
PLACEMENT_RANK = {
    "named": 6,      # a human-named event folder — the most valuable placement there is
    "day": 5,        # a dd.mm.yy folder: finer-grained than YYYY-MM, and hand-made
    "curated": 4,    # Albums / Archives / Camera — deliberately filed
    "bucket": 3,     # YYYY-MM — reconstructible from capture date
    "stub": 2,       # YYYY-00 — a placeholder, weaker than a real month
    "undated": 1,    # _Undated — no date at all
    "staging": 0,    # To Sort / Import — nobody has filed this yet
}


def placement_kind(rel: str, date_root: str = "By Date",
                   loose: set[str] | None = None) -> str:
    """The PLACEMENT_RANK key for a file's location."""
    rel = rel.replace("\\", "/")
    loose = loose or {"to sort", "unsorted", "inbox", "import", "staging"}
    top = rel.split("/", 1)[0]
    in_date_tree = rel.lower().startswith(date_root.replace("\\", "/").lower() + "/")
    if in_date_tree:
        parts = rel.split("/")
        if len(parts) > 1 and parts[1] == "_Undated":
            return "undated"
        place = placement(rel, date_root)
        if place in ("named", "day", "bucket", "stub"):
            return place
        return "undated"
    if top.lower() in loose:
        return "staging"
    return "curated"


def placement_rank(rel: str, date_root: str = "By Date",
                   loose: set[str] | None = None) -> int:
    """Higher = a placement worth preserving. Feed to dedup as its keeper preference."""
    return PLACEMENT_RANK.get(placement_kind(rel, date_root, loose), 0)


def current_bucket(rel: str, date_root: str = "By Date") -> str | None:
    m = _year_re(date_root).match(rel.replace("\\", "/"))
    if m and YM_RE.match(m.group(2)):
        return f"{m.group(1)}/{m.group(2)}"
    return None


def exif_bucket(rec: dict, max_year: int | None = None) -> str | None:
    """``YYYY/YYYY-MM`` from a REAL capture date only.

    Deliberately does NOT fall back to mtime or to probe's ``year`` field: on a
    recovered archive those are noise, and filing a photo under a wrong year is
    worse than leaving it undated.
    """
    max_year = max_year or time.localtime().tm_year
    digits = "".join(ch for ch in str(rec.get("created") or "") if ch.isdigit())
    if len(digits) >= 6:
        y, mo = digits[:4], digits[4:6]
        if "1900" <= y <= str(max_year) and "01" <= mo <= "12":
            return f"{y}/{y}-{mo}"
    return None


def fname_bucket(name: str, max_year: int | None = None) -> str | None:
    """``YYYY/YYYY-MM`` from camera/phone filename conventions (IMG_20150817_…)."""
    max_year = max_year or time.localtime().tm_year
    for rx in FNAME_DATE:
        m = rx.search(name)
        if not m:
            continue
        y, mo = m.group(1) + m.group(2), m.group(3)
        if "1990" <= y <= str(max_year) and "01" <= mo <= "12":
            return f"{y}/{y}-{mo}"
    return None


def is_junk(rel: str, name: str, ext: str, size: int) -> str | None:
    """Why this file is litter, or None if it is not."""
    if JUNK_DIR_RE.search("/" + rel.replace("\\", "/")):
        return "cache folder"
    if name.lower() in JUNK_NAME:
        return "OS litter"
    if ext in JUNK_EXT:
        return f"litter ext {ext}"
    if size == 0:
        # An empty MEDIA file is a broken remnant. An empty .txt is not: libraries
        # use zero-byte text files as folder captions, where the filename IS the note.
        if ext in CAPTION_EXT:
            return None
        return "zero-byte"
    return None


# ── plan ────────────────────────────────────────────────────────────────────
def _load_jsonl(path: Path, key: str = "rel") -> list[dict]:
    out, seen = [], set()
    if not path.exists():
        return out
    for line in path.open(encoding="utf-8", errors="replace"):
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        k = r.get(key)
        if k and k not in seen:
            seen.add(k)
            out.append(r)
    return out


def _load_autotrash(root: Path, *, pixel_dupes: bool = True) -> set[str]:
    """rels dedup flagged as redundant copies (the keeper is always excluded).

    ``exact`` is byte-identical. ``identical-pixels`` decodes to the same image while
    differing in container or metadata — still the same photo, and invisible to
    byte-hashing, so it is included by default; ``pixel_dupes=False`` restricts this to
    byte-identical copies only. ``near-image``/``near-video`` are never auto-trashed:
    those are genuinely different pixels and stay a human decision.
    """
    kinds = {"exact", "identical-pixels"} if pixel_dupes else {"exact"}
    s: set[str] = set()
    dp = root / ".mediaexplorer" / "dupes.jsonl"
    if not dp.exists():
        return s
    for line in dp.open(encoding="utf-8", errors="replace"):
        try:
            g = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if g.get("kind") in kinds:
            s.update(g.get("auto_trash", []))
    return s


def walk_all(root: Path, skip: set[str]) -> list[tuple[str, str, str, int]]:
    """(abs, rel, ext, size) for every file — probe only indexes MEDIA, so litter
    is invisible to meta.jsonl and must come from a real filesystem walk."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
        dirnames[:] = [d for d in dirnames if d.lower() not in skip]
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            try:
                size = os.path.getsize(p)
            except OSError:
                size = 0
            out.append((p, os.path.relpath(p, root).replace("\\", "/"),
                        os.path.splitext(fn)[1].lower(), size))
    return out


def build_plan(root: Path, *, date_root: str = "By Date",
               video_dest: Path | None = None,
               loose: set[str] | None = None,
               tiny_max: int = 20 * 1024,
               recovered_dir: str = "_Recovered",
               skip_tiny: bool = False,
               pixel_dupes: bool = True) -> tuple[list[dict], dict]:
    """Compute every move. Pure calculation — touches nothing on disk.

    ``loose`` names top-level folders that are explicitly unsorted staging areas
    (e.g. ``To Sort``). Everything outside ``date_root`` and ``loose`` is treated as
    curated and is only ever touched for litter/duplicates, never re-dated.
    """
    root = Path(root).resolve()
    loose = {s.strip("/\\").lower() for s in (loose or {"to sort", "unsorted", "inbox"})}
    trash = root / ".trash"
    skip_dirs = {".trash", ".mediaexplorer", "$recycle.bin",
                 "system volume information", recovered_dir.lower()}

    meta = {r["rel"]: r for r in _load_jsonl(root / ".mediaexplorer" / "meta.jsonl")}
    autotrash = _load_autotrash(root, pixel_dupes=pixel_dupes)

    rows: list[dict] = []
    stats: dict[str, int] = defaultdict(int)
    nbytes: dict[str, int] = defaultdict(int)

    def emit(action, reason, size, src, dst):
        rows.append({"action": action, "reason": reason, "size": size,
                     "src": str(src), "dst": str(dst)})
        stats[action] += 1
        nbytes[action] += size

    files = walk_all(root, skip_dirs)
    video_dirs = {os.path.dirname(rel) for _, rel, ext, _ in files if ext in VIDEO_EXT}

    for abs_p, rel, ext, size in files:
        rec = meta.get(rel, {})
        top = rel.split("/", 1)[0]

        why = is_junk(rel, os.path.basename(rel), ext, size)
        if why:
            emit("trash-junk", why, size, abs_p, trash / "junk" / rel)
            continue

        if rel in autotrash:
            emit("trash-dupe", "exact duplicate", size, abs_p, trash / "dupes" / rel)
            continue

        if video_dest and (ext in VIDEO_EXT
                           or (ext in VIDEO_SIDECAR_EXT
                               and os.path.dirname(rel) in video_dirs)):
            b = (exif_bucket(rec) or fname_bucket(os.path.basename(rel))
                 or (f"{rec['year']}/{rec['year']}-00" if rec.get("year") else "_Undated"))
            emit("move-video", "video out of photo library", size, abs_p,
                 Path(video_dest) / b.replace("/", os.sep) / os.path.basename(rel))
            continue

        is_image = rec.get("type") == "image"
        place = placement(rel, date_root)
        in_date_tree = rel.lower().startswith(date_root.replace("\\", "/").lower() + "/")
        # Only the date tree and the declared staging areas may be reshaped. Everything
        # else is a curated collection: the operator put those files there on purpose.
        eligible = in_date_tree or top.lower() in loose

        # A small image inside a curated album was KEPT deliberately — sweeping it as
        # "thumbnail litter" would quietly gut hand-made collections. Only the piles
        # nobody has sorted yet get the size filter.
        if (is_image and not skip_tiny and size < tiny_max
                and eligible and place not in ("named", "day")):
            emit("trash-tiny", f"{size}B thumbnail", size, abs_p, trash / "tiny" / rel)
            continue

        # curated placements are sacred — see rule 1 in the module docstring
        if place in ("named", "day"):
            stats[f"keep-{place}"] += 1
            continue

        if not eligible:
            stats["keep-curated"] += 1
            continue

        b = exif_bucket(rec) or fname_bucket(os.path.basename(rel))
        cur = current_bucket(rel, date_root)

        if place == "bucket" and (b is None or b == cur):
            stats["keep-in-place"] += 1
            continue

        if place == "stub":
            stub_year = rel.replace("\\", "/").split("/")[1]
            if not b or not b.startswith(stub_year + "/"):
                stats["keep-stub"] += 1
                continue

        if b:
            dst = root / date_root / b.replace("/", os.sep) / os.path.basename(rel)
            if os.path.normcase(str(dst)) == os.path.normcase(abs_p):
                stats["already-correct"] += 1
                continue
            emit("redate", "EXIF/filename capture date", size, abs_p, dst)
            continue

        if RECOVERED_RE.match(os.path.basename(rel)):
            emit("quarantine", "recovery salvage, no capture date", size, abs_p,
                 root / recovered_dir / os.path.basename(rel))
        elif place == "loose" and not rel.lower().startswith(
                f"{date_root.lower()}/_undated/"):
            emit("redate", "undated, consolidated", size, abs_p,
                 root / date_root / "_Undated" / os.path.basename(rel))
        else:
            stats["keep-undated"] += 1

    summary = {k: stats[k] for k in sorted(stats)}
    summary |= {f"{k}_gb": round(v / 1024**3, 2) for k, v in sorted(nbytes.items()) if v}
    return rows, summary


def write_plan(rows: list[dict], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["action", "reason", "size", "src", "dst"])
        w.writeheader()
        w.writerows(rows)
    return path


# ── apply / undo ────────────────────────────────────────────────────────────
def _unique(dst: Path) -> Path:
    if not dst.exists():
        return dst
    stem, suf, i = dst.stem, dst.suffix, 1
    while (c := dst.with_name(f"{stem} ({i}){suf}")).exists():
        i += 1
    return c


#: trash first — those moves VACATE paths that a later redate wants to land on
_ORDER = {"trash-junk": 0, "trash-tiny": 1, "trash-dupe": 2,
          "move-video": 3, "redate": 4, "quarantine": 5}


def apply_plan(rows: list[dict], log_path: Path, *, only: set[str] | None = None,
               quiet: bool = False) -> dict:
    """Execute the plan as same-volume renames, appending to an undo log as it goes.

    ``os.rename`` (never ``os.replace``) plus ``_unique`` means an existing file at
    the destination is never overwritten. The log is flushed per move, so a run
    killed halfway is still completely reversible.
    """
    def log(*a):
        if not quiet:
            print(*a, flush=True)

    rows = [r for r in rows if not only or r["action"] in only]
    rows.sort(key=lambda r: _ORDER.get(r["action"], 99))
    st: dict[str, int] = defaultdict(int)
    moved = 0
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", newline="", encoding="utf-8-sig") as lf:
        w = csv.writer(lf)
        w.writerow(["action", "moved_to", "original_src"])
        for i, r in enumerate(rows, 1):
            src, dst = Path(r["src"]), Path(r["dst"])
            if not src.exists():
                st[f"{r['action']}:gone"] += 1     # an earlier run already did it
                continue
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                real = _unique(dst)
                size = src.stat().st_size
                os.rename(src, real)
                w.writerow([r["action"], str(real), str(src)])
                lf.flush()
                st[r["action"]] += 1
                moved += size
            except Exception as e:  # noqa: BLE001
                st[f"{r['action']}:ERROR"] += 1
                log(f"  [photos] ERROR {src} -> {dst}: {e}")
            if i % 2000 == 0:
                log(f"  [photos] {i}/{len(rows)}  {moved/1024**3:.1f} GB")
    st["moved_gb"] = round(moved / 1024**3, 2)
    return dict(st)


def undo(log_path: Path, quiet: bool = False) -> dict:
    """Put every file a run moved back where it came from (newest move first)."""
    with Path(log_path).open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    st: dict[str, int] = defaultdict(int)
    for r in reversed(rows):
        cur, orig = Path(r["moved_to"]), Path(r["original_src"])
        if not cur.exists():
            st["gone"] += 1
            continue
        try:
            orig.parent.mkdir(parents=True, exist_ok=True)
            os.rename(cur, _unique(orig))
            st["restored"] += 1
        except Exception as e:  # noqa: BLE001
            st["ERROR"] += 1
            if not quiet:
                print(f"  [photos] ERROR restoring {cur}: {e}", flush=True)
    return dict(st)


def prune_empty_dirs(root: Path, *, apply: bool = False,
                     skip: set[str] | None = None) -> list[str]:
    """Directories containing no files at any depth. Removes DIRECTORIES ONLY."""
    skip = skip or {".trash", ".mediaexplorer", "$recycle.bin",
                    "system volume information"}
    removed: list[str] = []
    for _ in range(12):
        found = []
        for dirpath, dirnames, filenames in os.walk(root, topdown=True,
                                                    onerror=lambda e: None):
            dirnames[:] = [d for d in dirnames if d.lower() not in skip]
            if dirpath == str(root):
                continue
            if not filenames and not dirnames:
                found.append(dirpath)
        if not found:
            break
        removed.extend(found)
        if not apply:
            break
        for d in found:
            try:
                os.rmdir(d)
            except OSError:
                pass
    return removed
