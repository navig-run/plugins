"""Copy-verify-trash migrator for navig-explore (the Y: -> X: consolidation).

Consumes ``meta.jsonl`` (from probe) + ``dupes.jsonl`` (from dedup) and routes
every media file to its destination under a master library, by PROVENANCE:

  images                      -> Photos/By Date/<YYYY>/<YYYY-MM>/
  audio (SoundEffects)        -> Audio/SFX/...
  audio (other)               -> Audio/...
  video _assets/*             -> Video/_assets/...
  video _projects/somaleto/*  -> Video/somaleto/<channel>/...   (structure kept)
  video _import|_filter/*     -> Video/camera/...
  video _IPHONE/* or phone    -> Video/phone/...
  video camera/drone class    -> Video/camera/...
  video RAW/* (published)     -> Video/episodes-raw/...
  everything else video       -> Video/incoming/...

Two modes, both idempotent and NON-DESTRUCTIVE of the library:
  --plan   walk the index, compute destinations, write a manifest CSV. NO file
           operation happens. This IS the review gate — nothing moves unreviewed.
  --apply  for each row: copy src -> dst, verify size + SHA-256, and only then
           move the *source* into ``<source-root>/.trash/<rel>`` (never deleted).
           A hash mismatch aborts that one file (source kept, logged). Re-running
           skips files whose dst already exists and verifies.

Exact-duplicate members (from dupes.jsonl, keep excluded) are routed straight to
``.trash`` rather than copied into the library.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus",
             ".wma", ".aif", ".aiff"}


def _load_meta(root: Path) -> list[dict]:
    mp = root / ".mediaexplorer" / "meta.jsonl"
    out, seen = [], set()
    if not mp.exists():
        return out
    for line in mp.open(encoding="utf-8", errors="replace"):
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        rel = r.get("rel")
        if rel and rel not in seen:
            seen.add(rel)
            out.append(r)
    return out


def _load_autotrash(root: Path) -> set[str]:
    """Rels that are exact-duplicate NON-keep members → go to .trash, not library."""
    dp = root / ".mediaexplorer" / "dupes.jsonl"
    s: set[str] = set()
    if not dp.exists():
        return s
    for line in dp.open(encoding="utf-8", errors="replace"):
        try:
            g = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if g.get("kind") == "exact":
            s.update(g.get("auto_trash", []))
    return s


def _date_bucket(rec: dict) -> str:
    """YYYY/YYYY-MM from capture date, else the file's year, else Unknown Date."""
    created = str(rec.get("created") or "")
    # normalise "2024:01:05 13:13:20" or "2024-01-05T..." → YYYY, MM
    digits = "".join(ch for ch in created if ch.isdigit())
    if len(digits) >= 6:
        y, m = digits[:4], digits[4:6]
        if "1900" <= y <= "2099" and "01" <= m <= "12":
            return f"{y}/{y}-{m}"
    yr = rec.get("year")
    if yr:
        return f"{yr}/{yr}-00"
    return "Unknown Date"


def _after(rel: str, *prefixes: str) -> str:
    """Return the path portion after the first matching top prefix (case-insensitive)."""
    low = rel.lower()
    for pre in prefixes:
        p = pre.lower().rstrip("/") + "/"
        if low.startswith(p):
            return rel[len(p):]
    # fall back: drop just the first segment
    parts = rel.split("/", 1)
    return parts[1] if len(parts) > 1 else rel


def route_dest(rec: dict) -> str:
    """Destination path RELATIVE to the master library root (posix)."""
    rel = rec["rel"]
    top = rel.split("/", 1)[0].lower()
    typ = rec.get("type")
    ext = rec.get("ext", "").lower()
    name = rec["name"]
    sc = rec.get("source_class", "")
    clue = (rec.get("clue") or "").lower()

    if typ == "image":
        return f"Photos/By Date/{_date_bucket(rec)}/{name}"

    if typ == "audio" or ext in AUDIO_EXT:
        if "soundeffect" in clue or "sfx" in clue or "sound effect" in clue:
            return f"Audio/SFX/{_after(rel, '_assets/SoundEffects', '_assets')}"
        return f"Audio/{_after(rel, '_assets')}" if top == "_assets" else f"Audio/{rel}"

    # video (and video-sidecars .lrv/.thm ride along by folder)
    if top == "_assets":
        return f"Video/_assets/{_after(rel, '_assets')}"
    if top == "raw":
        return f"Video/episodes-raw/{_after(rel, 'RAW', 'raw')}"
    if top == "_projects" and "somaleto" in rel.lower():
        return f"Video/somaleto/{_after(rel, '_projects/somaleto')}"
    if top == "_projects":
        return f"Video/somaleto/_other/{_after(rel, '_projects')}"
    if top in ("_import", "_filter"):
        return f"Video/camera/{_after(rel, top)}"
    if top == "_iphone":
        return f"Video/phone/{_after(rel, '_IPHONE', '_iphone')}"
    if sc == "phone":
        return f"Video/phone/{_after(rel, top) if top.startswith('_') else rel}"
    if sc in ("camera", "drone"):
        return f"Video/camera/{_after(rel, top) if top.startswith('_') else rel}"
    return f"Video/incoming/{rel}"


# ── hashing / verify ────────────────────────────────────────────────────────
def _sha256(path: str) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:  # noqa: BLE001
        return None


def _unique(dst: Path) -> Path:
    if not dst.exists():
        return dst
    stem, suf, i = dst.stem, dst.suffix, 1
    while (c := dst.with_name(f"{stem} ({i}){suf}")).exists():
        i += 1
    return c


def _copy_verify(src: str, tmp: Path) -> bool:
    """Streaming copy src->tmp, hashing the source in the SAME read pass, then verify
    by hashing tmp once. One src read + one tmp read (vs. copy2 + two full src reads)
    — same SHA-256 guarantee, ~25% less I/O. Preserves mtime/mode like copy2. Returns
    True iff the written file is bit-identical to the source."""
    try:
        h = hashlib.sha256()
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            for chunk in iter(lambda: fi.read(1 << 20), b""):
                h.update(chunk)
                fo.write(chunk)
        shutil.copystat(src, tmp)
    except Exception:  # noqa: BLE001
        try:
            tmp.unlink()
        except Exception:  # noqa: BLE001
            pass
        return False
    return h.hexdigest() == _sha256(str(tmp))


# ── plan ────────────────────────────────────────────────────────────────────
def plan(root: Path, dest_root: Path, out_csv: Path, quiet=False,
         only_type: str | None = None, only_top: str | None = None) -> dict:
    def log(*a):
        if not quiet:
            print(*a, flush=True)

    recs = [r for r in _load_meta(root) if _match(r, only_type, only_top)]
    autotrash = _load_autotrash(root)
    log(f"[route] planning {len(recs)} files from {root} → {dest_root} "
        f"({len(autotrash)} exact-dupes → trash)")
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    stats = {"copy": 0, "trash-dupe": 0}
    bytes_copy = 0
    with out_csv.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["action", "type", "source_class", "res_tier", "year", "size",
                    "src", "dest_rel"])
        for r in recs:
            rel = r["rel"]
            if rel in autotrash:
                action, dest = "trash-dupe", f".trash/{rel}"
                stats["trash-dupe"] += 1
            else:
                action, dest = "copy", route_dest(r)
                stats["copy"] += 1
                bytes_copy += r.get("size") or 0
            w.writerow([action, r.get("type"), r.get("source_class"),
                        r.get("res_tier"), r.get("year"), r.get("size"),
                        r.get("abs") or str(root / rel), dest])
    stats["copy_gb"] = round(bytes_copy / 1024**3, 1)
    stats["manifest"] = str(out_csv)
    log(f"[route] plan: {json.dumps(stats, ensure_ascii=False)}")
    return stats


# ── apply ───────────────────────────────────────────────────────────────────
def _match(r: dict, only_type, only_top) -> bool:
    if only_type and r.get("type") != only_type:
        return False
    if only_top and r["rel"].split("/", 1)[0].lower() != only_top.lower():
        return False
    return True


def apply(root: Path, dest_root: Path, quiet=False, limit: int | None = None,
          verify=True, only_type: str | None = None, only_top: str | None = None) -> dict:
    def log(*a):
        if not quiet:
            print(*a, flush=True)

    recs = [r for r in _load_meta(root) if _match(r, only_type, only_top)]
    autotrash = _load_autotrash(root)
    trash_root = root / ".trash"
    st = {"copied": 0, "verified": 0, "skipped": 0, "trashed": 0,
          "mismatch": 0, "error": 0}
    log(f"[route] apply {len(recs)} files"
        f"{' type='+only_type if only_type else ''}{' top='+only_top if only_top else ''}"
        f" · {root} -> {dest_root}")
    done = 0
    for r in recs:
        rel = r["rel"]
        src = r.get("abs") or str(root / rel)
        if not os.path.exists(src):
            st["skipped"] += 1
            continue
        try:
            if rel in autotrash:
                _to_trash(src, root, rel, trash_root)
                st["trashed"] += 1
            else:
                _migrate_one(src, dest_root / route_dest(r), root, rel,
                             trash_root, verify, st)
        except Exception as e:  # noqa: BLE001
            st["error"] += 1
            log(f"  [route] ERROR {rel}: {e}")
        done += 1
        if done % 200 == 0:
            log(f"  [route] {done}/{len(recs)}  {json.dumps(st)}")
        if limit and done >= limit:
            break
    log(f"[route] apply done: {json.dumps(st, ensure_ascii=False)}")
    return st


def _to_trash(src: str, root: Path, rel: str, trash_root: Path):
    dst = _unique(trash_root / rel)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(src, str(dst))


# ── mirror mode (structure-preserving, for the already-curated G: library) ───
MIRROR_SKIP = {".mediaexplorer", ".trash", "_trash", "$recycle.bin",
               "system volume information", "_organized"}


def _do_migrate(src: str, dst: Path, trash_dst: Path, verify: bool, st: dict):
    """Copy src->dst, verify size+SHA-256, then move src into trash_dst. A bad
    copy leaves the source untouched (never trashed). Idempotent: an already
    present+matching dst just trashes the leftover source."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and os.path.getsize(dst) == os.path.getsize(src):
        if (not verify) or _sha256(str(dst)) == _sha256(src):
            trash_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(src, str(_unique(trash_dst)))
            st["skipped"] += 1
            return
    real_dst = _unique(dst)
    tmp = real_dst.with_suffix(real_dst.suffix + ".copytmp")
    if verify:
        st["copied"] += 1
        if not _copy_verify(src, tmp):   # streaming copy + hash-verify in fewer reads
            st["mismatch"] += 1
            return  # SOURCE KEPT — bad copy, tmp already removed by _copy_verify
        st["verified"] += 1
    else:
        shutil.copy2(src, tmp)
        st["copied"] += 1
    os.replace(tmp, real_dst)
    trash_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(src, str(_unique(trash_dst)))
    st["trashed"] += 1


def mirror_apply(src_root: Path, dst_root: Path, trash_root: Path, *,
                 prefix: str | None = None, verify: bool = True,
                 limit: int | None = None, quiet: bool = False) -> dict:
    """Mirror src_root -> dst_root preserving structure (copy-verify-trash).
    Trash lands under trash_root/<prefix>/<rel> (prefix defaults to src_root's
    name, so G:\\Audio originals -> G:\\.trash\\Audio\\...)."""
    src_root, dst_root = Path(src_root).resolve(), Path(dst_root).resolve()
    prefix = prefix if prefix is not None else src_root.name
    st = {"copied": 0, "verified": 0, "skipped": 0, "trashed": 0,
          "mismatch": 0, "error": 0, "bytes": 0}

    def log(*a):
        if not quiet:
            print(*a, flush=True)

    log(f"[mirror] {src_root} -> {dst_root}  (trash: {trash_root}\\{prefix})")
    # Walk to completion BEFORE migrating anything. `_do_migrate` moves each source into
    # the trash — deleting entries from the very directories os.walk is still enumerating.
    # os.walk makes no promise about a tree that changes underneath it, and the failure it
    # would produce is the bad kind: files silently skipped, no error, no count, a run that
    # reports success having quietly missed some. This has not been observed here; the
    # separation is cheap insurance, and it also lets the run report a total up front.
    sources: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(src_root, onerror=lambda e: None,
                                                followlinks=False):
        dirnames[:] = [d for d in dirnames if d.lower() not in MIRROR_SKIP]
        for fn in filenames:
            src = os.path.join(dirpath, fn)
            sources.append((src, os.path.relpath(src, src_root)))
    log(f"[mirror] {len(sources)} files to migrate")

    done = 0
    for src, rel in sources:
        try:
            dst = dst_root / rel
            trash_dst = trash_root / prefix / rel
            sz = os.path.getsize(src)
            _do_migrate(src, dst, trash_dst, verify, st)
            st["bytes"] += sz
        except Exception as e:  # noqa: BLE001
            st["error"] += 1
            log(f"  [mirror] ERROR {rel}: {e}")
        done += 1
        if done % 250 == 0:
            log(f"  [mirror] {done}/{len(sources)} · {st['bytes']/1024**3:.1f} GB · {json.dumps(st)}")
        if limit and done >= limit:
            log(f"[mirror] hit limit {limit}")
            log(f"[mirror] done: {json.dumps(st, ensure_ascii=False)}")
            return st
    log(f"[mirror] done: {json.dumps(st, ensure_ascii=False)}")
    return st


def _migrate_one(src: str, dst: Path, root: Path, rel: str, trash_root: Path,
                 verify: bool, st: dict):
    dst.parent.mkdir(parents=True, exist_ok=True)
    # idempotent: already migrated + same size (and hash if verifying) → just trash src
    if dst.exists() and os.path.getsize(dst) == os.path.getsize(src):
        if (not verify) or _sha256(str(dst)) == _sha256(src):
            _to_trash(src, root, rel, trash_root)
            st["skipped"] += 1
            return
    real_dst = _unique(dst)
    tmp = real_dst.with_suffix(real_dst.suffix + ".copytmp")
    if verify:
        st["copied"] += 1
        if not _copy_verify(src, tmp):   # streaming copy + hash-verify in fewer reads
            st["mismatch"] += 1
            return  # SOURCE KEPT — bad copy, tmp already removed by _copy_verify — never trash on a bad copy
        st["verified"] += 1
    else:
        shutil.copy2(src, tmp)
        st["copied"] += 1
    os.replace(tmp, real_dst)
    _to_trash(src, root, rel, trash_root)  # source retained in .trash, not deleted
    st["trashed"] += 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Route media src->dst (copy-verify-trash)")
    ap.add_argument("root", help="Source root (e.g. Y:\\)")
    ap.add_argument("--dest", required=True, help="Master library root (e.g. X:\\)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--plan", action="store_true", help="write manifest CSV only")
    g.add_argument("--apply", action="store_true", help="execute copy-verify-trash")
    ap.add_argument("--manifest", default=None, help="manifest CSV path (--plan)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--only-type", default=None, help="image|audio|video")
    ap.add_argument("--only-top", default=None, help="restrict to a top Y: folder e.g. _assets")
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    root, dest = Path(a.root).resolve(), Path(a.dest).resolve()
    t0 = time.time()
    if a.plan:
        suffix = a.only_type or a.only_top or "all"
        mani = Path(a.manifest) if a.manifest else root / ".mediaexplorer" / f"route-plan-{suffix}.csv"
        plan(root, dest, mani, quiet=a.quiet, only_type=a.only_type, only_top=a.only_top)
    else:
        apply(root, dest, quiet=a.quiet, limit=a.limit, verify=not a.no_verify,
              only_type=a.only_type, only_top=a.only_top)
    if not a.quiet:
        print(f"[route] {time.time()-t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
