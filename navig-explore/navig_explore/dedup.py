"""Duplicate detection for navig-explore.

Consumes the ``meta.jsonl`` sidecar(s) written by ``probe.py`` and emits a
``dupes.jsonl`` report the explorer surfaces as a filter. Two independent passes:

  EXACT  (byte-identical) — the ONLY class eligible for auto-trash. Found cheaply
         by size-bucketing first (files of different size can't be byte-equal),
         then a quick head+tail hash, then a full SHA-256 only to CONFIRM a
         quick-hash collision. Reading 3.4 TB in full is avoided — only genuine
         same-size candidates are ever fully hashed.

  NEAR   (perceptual / re-encode / resize) — FLAG-ONLY, never auto-removed
         (operator decision). Images: perceptual hash (imagehash.phash) grouped
         by Hamming distance. Videos: a cheap signature of duration(0.1s) +
         WxH — same-source re-encodes share it; the human confirms in the
         Explorer. (Optional ``--video-phash`` adds a 1s-frame perceptual hash.)

Within every group a ``keep`` is chosen (highest resolution → longest → largest →
the copy whose name is NOT a "… (2)"/"copy" clone and whose path is shortest / not
under an ``old``/``TRASH`` folder). For EXACT groups the non-keep members are the
``auto_trash`` set.

Cross-root: pass several roots to dedup Y: against the existing G:/X: library — a
Y: file byte-identical to one already in the library is reported so it isn't
migrated twice.

Runnable standalone:

    python dedup.py <root> [<root2> ...] [--near-image-dist 6] [--video-phash]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_RE_COPY = re.compile(r"(\(\d+\)|[-_ ]copy|copy[-_ ]|[-_ ]\d+ *$)", re.I)
_BAD_LOC = re.compile(r"(^|/)(old|trash|_trash|videos_fltr_old|@import|to sort|tosort)(/|$)", re.I)
_QUICK = 1 << 20  # 1 MiB head + 1 MiB tail for the quick hash
_WORKERS = 16     # hashing/decoding is I/O-bound; both release the GIL


def _load_meta(root: Path) -> list[dict]:
    """Load a root's meta.jsonl; annotate each record with its root for reporting."""
    mp = root / ".mediaexplorer" / "meta.jsonl"
    recs, seen = [], set()
    if not mp.exists():
        return recs
    for line in mp.open(encoding="utf-8", errors="replace"):
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        rel = r.get("rel")
        if not rel or rel in seen:      # last-write-wins on resume dupes
            continue
        seen.add(rel)
        r["root"] = str(root)
        r["_abs"] = r.get("abs") or str(root / rel)
        recs.append(r)
    return recs


def quick_hash(path: str, size: int) -> str | None:
    """sha1 of (size, first 1 MiB, last 1 MiB) — cheap discriminator."""
    try:
        h = hashlib.sha1(str(size).encode())
        with open(path, "rb") as f:
            h.update(f.read(_QUICK))
            if size > 2 * _QUICK:
                f.seek(-_QUICK, os.SEEK_END)
                h.update(f.read(_QUICK))
        return h.hexdigest()
    except Exception:  # noqa: BLE001
        return None


def full_hash(path: str) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:  # noqa: BLE001
        return None


def _keep_score(r: dict) -> tuple:
    """Higher = better original to KEEP. Prefer resolution, duration, size, a
    non-clone name, a non-junk location, and a shorter path."""
    w, h = r.get("w") or 0, r.get("h") or 0
    name = r.get("name", "")
    rel = r.get("rel", "")
    return (
        w * h,
        r.get("dur") or 0,
        r.get("size") or 0,
        0 if _RE_COPY.search(os.path.splitext(name)[0]) else 1,
        0 if _BAD_LOC.search("/" + rel.lower()) else 1,
        -len(rel),
    )


def _pick_keep(members: list[dict], rank=None) -> dict:
    """The copy to KEEP.

    ``rank`` (rel -> int, higher = better) is consulted BEFORE anything else. It exists
    because the intrinsic score cannot express what a library's own structure means: when
    two files are the same image, every resolution/duration term ties and SIZE decides,
    which amounts to "keep whichever copy has more EXIF". On a curated photo library that
    is actively wrong — it discards the copy filed under a human-named event and keeps the
    one in a date bucket that capture metadata can rebuild for free.
    """
    if rank is None:
        return max(members, key=_keep_score)
    return max(members, key=lambda r: (rank(r.get("rel", "")), *_keep_score(r)))


def find_exact(recs: list[dict], log, workers: int = _WORKERS, rank=None) -> list[dict]:
    """Byte-identical groups, narrowed size → quick-hash → SHA-256.

    Both hash stages run on a thread pool: this is pure I/O wait (the bottleneck is
    the disk, not Python) and hashlib releases the GIL while it works, so threads
    genuinely parallelise it. On a 162k-image library over USB the serial version
    took hours; this takes minutes.

    Quick-hashing is done across ALL size-candidates at once rather than per size
    group. That is equivalent — quick_hash seeds the digest with the size, so two
    files of different sizes cannot collide — and it keeps the pool saturated
    instead of draining it at every group boundary.
    """
    by_size = defaultdict(list)
    for r in recs:
        if r.get("size"):
            by_size[r["size"]].append(r)
    candidates = [r for g in by_size.values() if len(g) > 1 for r in g]
    log(f"[dedup] exact: {sum(1 for g in by_size.values() if len(g) > 1)} size-groups, "
        f"{len(candidates)} candidate files to quick-hash ({workers} workers)")

    with ThreadPoolExecutor(workers) as ex:
        quick = list(ex.map(lambda r: quick_hash(r["_abs"], r["size"]), candidates))
    by_quick = defaultdict(list)
    for r, qh in zip(candidates, quick):
        if qh:
            by_quick[qh].append(r)

    confirm = [r for g in by_quick.values() if len(g) > 1 for r in g]
    log(f"[dedup] exact: {len(confirm)} files survive quick-hash → SHA-256")
    with ThreadPoolExecutor(workers) as ex:
        full = list(ex.map(lambda r: full_hash(r["_abs"]), confirm))
    by_full = defaultdict(list)
    for r, fh in zip(confirm, full):
        if fh:
            by_full[fh].append(r)

    groups: list[dict] = []
    for fh, members in by_full.items():
        if len(members) < 2:
            continue
        keep = _pick_keep(members, rank)
        groups.append({
            "kind": "exact", "key": fh, "size": keep["size"],
            "members": [m["rel"] for m in members],
            "abs": [m["_abs"] for m in members],
            "keep": keep["rel"],
            "auto_trash": [m["rel"] for m in members if m["rel"] != keep["rel"]],
        })
    dupes = sum(len(g["auto_trash"]) for g in groups)
    reclaim = sum(g["size"] * len(g["auto_trash"]) for g in groups)
    log(f"[dedup] exact: {len(groups)} groups, {dupes} duplicate files, "
        f"{reclaim/1024**3:.1f} GB reclaimable")
    return groups


def find_image_dupes(recs: list[dict], dist: int, log, workers: int = _WORKERS,
                     already_trashed: set[str] | None = None, rank=None) -> list[dict]:
    """Decode every image ONCE and derive two independent duplicate classes from it.

      IDENTICAL-PIXELS — the decoded images are byte-for-byte equal; only the container
        or its metadata differs. This is genuinely the SAME photo, not a similar one, so
        (like ``exact``) it carries an ``auto_trash`` list. Byte-hashing cannot see these:
        re-saving a JPEG, or a tool writing an EXIF tag, changes the file while leaving
        every pixel untouched. On a real family album 184 of 210 name collisions were
        exactly this — SHA-256 alone would have kept all 184 as "(1)" twins of images
        already in the library.

      NEAR-IMAGE — perceptually close (resize, crop, re-encode). Genuinely different
        pixels, so FLAG-ONLY, never auto-trashed: that stays a human decision.

    Both come from the same decode, so identical-pixel detection costs no extra I/O —
    and decoding is what dominates this pass on a large library.

    ``already_trashed`` are rels the byte-exact pass has already claimed; skipping them
    keeps this report to what byte-hashing MISSED rather than restating it. A file that
    cannot be decoded is dropped from both classes: unreadable must never mean redundant.
    """
    try:
        from PIL import Image                 # noqa: PLC0415
    except Exception:  # noqa: BLE001
        log("[dedup] image: Pillow unavailable — skipping image passes")
        return []
    # imagehash is only needed for perceptual CLUSTERING. Identical-pixel detection
    # needs nothing but a decoder, so a missing imagehash must not disable it — on this
    # machine imagehash is absent, and gating both on it would have silently shipped a
    # feature that never runs.
    try:
        import imagehash                      # noqa: PLC0415
    except Exception:  # noqa: BLE001
        imagehash = None
        log("[dedup] near-image: imagehash unavailable — perceptual clustering off "
            "(identical-pixel detection still runs)")
    already_trashed = already_trashed or set()
    imgs = [r for r in recs
            if r.get("type") == "image" and r.get("rel") not in already_trashed]
    log(f"[dedup] image: decoding {len(imgs)} images ({workers} workers) "
        f"— phash + pixel digest in one pass")

    def _digest(r):
        """(phash, pixel-digest) or (None, None) if it will not decode."""
        try:
            with Image.open(r["_abs"]) as im:
                ph = imagehash.phash(im) if imagehash else None
                rgb = im.convert("RGB")
                px = hashlib.sha1(f"{rgb.size[0]}x{rgb.size[1]}".encode()
                                  + rgb.tobytes()).hexdigest()
            return ph, px
        except Exception:  # noqa: BLE001
            return None, None

    with ThreadPoolExecutor(workers) as ex:
        digests = list(ex.map(_digest, imgs))

    groups: list[dict] = []

    # ── identical pixels ────────────────────────────────────────────────────
    by_px: dict[str, list[dict]] = defaultdict(list)
    for r, (_ph, px) in zip(imgs, digests):
        if px:
            by_px[px].append(r)
    pixel_dupe_rels: set[str] = set()
    for px, members in by_px.items():
        if len(members) < 2:
            continue
        # pixels are equal, so _keep_score's resolution/duration terms tie and SIZE
        # decides — which keeps the copy carrying the most metadata. That is the one
        # worth keeping when the only difference between two files is their EXIF.
        keep = _pick_keep(members, rank)
        groups.append({
            "kind": "identical-pixels", "key": px, "size": keep.get("size") or 0,
            "members": [m["rel"] for m in members],
            "abs": [m["_abs"] for m in members],
            "keep": keep["rel"],
            "auto_trash": [m["rel"] for m in members if m["rel"] != keep["rel"]],
        })
        pixel_dupe_rels.update(m["rel"] for m in members)
    n_px = sum(len(g["auto_trash"]) for g in groups)
    reclaim = sum(g["size"] * len(g["auto_trash"]) for g in groups)
    log(f"[dedup] identical-pixels: {len(groups)} groups, {n_px} same-image copies "
        f"byte-hashing missed, {reclaim/1024**3:.1f} GB")

    # ── perceptual neighbours (excluding what was already pixel-identical) ──
    hashes = [(int(str(ph), 16), ph, r) for r, (ph, _px) in zip(imgs, digests)
              if ph is not None and r["rel"] not in pixel_dupe_rels]
    near = 0
    used: set[int] = set()
    hashes.sort(key=lambda t: t[0])
    for i in range(len(hashes)):
        if id(hashes[i][2]) in used:
            continue
        _, hi, ri = hashes[i]
        cluster = [ri]
        for j in range(i + 1, len(hashes)):
            if id(hashes[j][2]) in used:
                continue
            if (hi - hashes[j][1]) <= dist:
                cluster.append(hashes[j][2])
                used.add(id(hashes[j][2]))
        if len(cluster) > 1:
            used.add(id(ri))
            keep = _pick_keep(cluster, rank)
            near += 1
            groups.append({
                "kind": "near-image", "key": str(hi),
                "members": [m["rel"] for m in cluster],
                "abs": [m["_abs"] for m in cluster],
                "keep": keep["rel"],
            })
    log(f"[dedup] near-image: {near} clusters flagged (review — not auto-removed)")
    return groups


#: previous name — kept so external callers/scripts do not break
find_near_images = find_image_dupes


def find_near_videos(recs: list[dict], log, rank=None) -> list[dict]:
    """Cheap signature: duration(0.1s) + WxH. Same-source re-encodes share it;
    flag-only for human review (may include unrelated same-length clips)."""
    vids = [r for r in recs if r.get("type") == "video" and (r.get("dur") or 0) > 0.5
            and r.get("w")]
    by_sig = defaultdict(list)
    for r in vids:
        sig = f"dur={round(r['dur'],1)}|{r['w']}x{r['h']}"
        by_sig[sig].append(r)
    groups = []
    for sig, members in by_sig.items():
        if len(members) < 2:
            continue
        keep = _pick_keep(members, rank)
        groups.append({
            "kind": "near-video", "key": sig,
            "members": [m["rel"] for m in members],
            "abs": [m["_abs"] for m in members],
            "keep": keep["rel"],
        })
    log(f"[dedup] near-video: {len(groups)} same-length+res clusters flagged "
        f"(review — not auto-removed)")
    return groups


def dedup(roots: list[Path], near_image_dist: int = 6, video_phash: bool = False,
          out: Path | None = None, quiet: bool = False, workers: int = _WORKERS,
          exact_only: bool = False, rank=None) -> dict:
    def log(*a):
        if not quiet:
            print(*a, flush=True)

    recs: list[dict] = []
    for root in roots:
        r = _load_meta(Path(root).resolve())
        log(f"[dedup] loaded {len(r)} records from {root}")
        recs.extend(r)
    if not recs:
        log("[dedup] no meta.jsonl found — run probe first")
        return {}

    out_path = Path(out) if out else Path(roots[0]).resolve() / ".mediaexplorer" / "dupes.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    exact = find_exact(recs, log, workers=workers, rank=rank)

    # Persist the exact groups NOW, before the perceptual passes. Those decode every
    # image and can run for hours on a large library; if the run is interrupted there,
    # the expensive-but-finished exact result must not die with it. The near passes
    # append to this file.
    def _write(groups, mode):
        with out_path.open(mode, encoding="utf-8") as f:
            for g in groups:
                f.write(json.dumps(g, ensure_ascii=False) + "\n")

    _write(exact, "w")
    log(f"[dedup] exact results written → {out_path}")

    img_groups: list[dict] = []
    near_vid: list[dict] = []
    if exact_only:
        log("[dedup] --exact-only: skipping the image decode pass "
            "(no identical-pixels or near-image detection)")
    else:
        already = {rel for g in exact for rel in g["auto_trash"]}
        img_groups = find_image_dupes(recs, near_image_dist, log, workers=workers,
                                      already_trashed=already, rank=rank)
        near_vid = find_near_videos(recs, log, rank=rank)
        _write((*img_groups, *near_vid), "a")

    pixel = [g for g in img_groups if g["kind"] == "identical-pixels"]
    near_img = [g for g in img_groups if g["kind"] == "near-image"]
    summary = {
        "exact_groups": len(exact),
        "exact_dupe_files": sum(len(g["auto_trash"]) for g in exact),
        "exact_reclaim_gb": round(sum(g["size"] * len(g["auto_trash"])
                                      for g in exact) / 1024**3, 1),
        "identical_pixel_groups": len(pixel),
        "identical_pixel_files": sum(len(g["auto_trash"]) for g in pixel),
        "identical_pixel_reclaim_gb": round(sum(g["size"] * len(g["auto_trash"])
                                                for g in pixel) / 1024**3, 1),
        "near_image_clusters": len(near_img),
        "near_video_clusters": len(near_vid),
        "out": str(out_path),
    }
    log(f"[dedup] SUMMARY: {json.dumps(summary, ensure_ascii=False)}")
    return summary


#: the classes where the files ARE the same image, so one copy is genuinely redundant.
#: near-image / near-video are deliberately absent: a resize or a crop is a different
#: picture, and on a real library those clusters turned out to be mostly consecutive
#: burst shots. Choosing between those is a human call, not a hash comparison.
QUARANTINABLE = ("exact", "identical-pixels")


def plan_quarantine(root: Path, trash: Path | None = None,
                    kinds: tuple[str, ...] = QUARANTINABLE) -> list[dict]:
    """Turn a written dupes.jsonl into move rows: redundant copy -> <root>/.trash/dupes.

    Groups are merged into connected components before deciding anything. They overlap
    in practice: a file can be byte-identical to one copy and pixel-identical to a third,
    so it is a *keeper* in one group and a *redundant member* in another. Treating groups
    independently either double-counts it (removing every copy) or spares it (leaving a
    redundant file behind that only a second run clears). Resolving the component once
    does neither: exactly ONE member of each set of same-image files survives, and the
    pass converges in a single run.

    The survivor is the file dedup preferred as a keeper most often — which, for equal
    pixels, is the copy carrying the most metadata — with the shortest path breaking ties
    so the choice is deterministic.

    Rows are the shape ``photos.apply_plan`` executes, which is what gives quarantine its
    per-move undo log for free. Members that no longer exist are skipped, so re-running
    after a partial run is safe.
    """
    root = Path(root).resolve()
    trash = Path(trash) if trash else root / ".trash" / "dupes"
    dp = root / ".mediaexplorer" / "dupes.jsonl"
    if not dp.exists():
        return []

    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    keep_votes: dict[str, int] = defaultdict(int)
    kind_of: dict[str, str] = {}
    for line in dp.open(encoding="utf-8", errors="replace"):
        try:
            g = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if g.get("kind") not in kinds:
            continue
        # `members` is what this module writes, but reconstruct it from keep+auto_trash
        # if a report lacks it: silently quarantining NOTHING would look like success.
        members = [m for m in g.get("members", []) if m]
        if not members:
            members = [m for m in [g.get("keep"), *g.get("auto_trash", [])] if m]
        if len(members) < 2:
            continue
        for m in members[1:]:
            union(members[0], m)
        keep_votes[g.get("keep", "")] += 1
        for m in members:
            kind_of.setdefault(m, g["kind"])

    components: dict[str, list[str]] = defaultdict(list)
    for rel in list(parent):
        components[find(rel)].append(rel)

    rows: list[dict] = []
    for members in components.values():
        present = [m for m in members if (root / m.replace("/", os.sep)).exists()]
        if len(present) < 2:
            continue                       # nothing left to deduplicate here
        survivor = max(present, key=lambda m: (keep_votes.get(m, 0), -len(m)))
        for rel in present:
            if rel == survivor:
                continue
            src = root / rel.replace("/", os.sep)
            rows.append({"action": f"trash-{kind_of.get(rel, 'duplicate')}",
                         "reason": f"same image as {survivor}",
                         "size": src.stat().st_size, "src": str(src),
                         "dst": str(trash / rel.replace("/", os.sep))})
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description="Detect exact + near duplicates → dupes.jsonl")
    ap.add_argument("roots", nargs="+", help="One or more probed roots (dedup spans them)")
    ap.add_argument("--near-image-dist", type=int, default=6)
    ap.add_argument("--video-phash", action="store_true",
                    help="(reserved) add 1s-frame perceptual hash for video near-dupes")
    ap.add_argument("--out", default=None, help="dupes.jsonl output path")
    ap.add_argument("--workers", type=int, default=_WORKERS,
                    help="parallel hash/decode workers")
    ap.add_argument("--exact-only", action="store_true",
                    help="skip the perceptual near-duplicate passes (much faster)")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    t0 = time.time()
    dedup([Path(r) for r in a.roots], near_image_dist=a.near_image_dist,
          video_phash=a.video_phash, out=Path(a.out) if a.out else None,
          quiet=a.quiet, workers=a.workers, exact_only=a.exact_only)
    if not a.quiet:
        print(f"[dedup] total {time.time()-t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
