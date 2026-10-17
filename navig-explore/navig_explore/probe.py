"""Media metadata probe for navig-explore.

Walks a folder and extracts per-file technical + capture metadata into a
``.mediaexplorer/meta.jsonl`` sidecar that ``explorer.build_index()`` folds into
its item index (keyed by the item's rel-path id):

  video  → ffprobe: width, height, codec, container, duration, fps, bitrate,
           creation_time, camera model
  image  → exiftool (batched): capture date, GPS, camera make/model, dimensions

It also derives two coarse buckets used for filtering:

  ``res_tier``     SD / HD / FHD / QHD / 4K / 8K   (from height)
  ``source_class`` phone / camera / drone / screen / old / unknown  (heuristic:
                   camera tags + filename patterns + folder-name clues + ext)

Design goals (mirrors the existing sidecar pattern in ``explorer.py``):
- **Resumable**: on rerun, rel-paths already in ``meta.jsonl`` are skipped, so a
  multi-hour scan of a 3 TB drive can be interrupted and continued.
- **Parallel**: ffprobe runs across a thread pool (I/O + subprocess bound);
  exiftool runs in big batches (one process per ~400 images).
- **Read-only**: never moves, deletes, or rewrites originals.
- **Robust**: a file that fails to probe is still recorded (``probe_ok=false``)
  so it appears in the index and the walk never aborts on one bad file.

Runnable standalone (no navig install needed for the scan itself):

    python probe.py <folder> [--workers 12] [--limit N] [--quiet]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ── file-type sets (superset of explorer.py, plus the formats seen on Y:) ────
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".heic", ".heif", ".tiff",
             ".tif", ".gif", ".mpo"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".flv", ".wmv",
             ".mts", ".m2ts", ".mpg", ".mpeg", ".3gp", ".lrv", ".mod", ".ts"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus",
             ".wma", ".aif", ".aiff"}
SKIP_DIRS = {".mediaexplorer", "_organized", ".trash", "_trash", "$recycle.bin",
             "system volume information", "__macosx"}
SIDECAR_EXT = {".thm", ".lrv", ".aae", ".xmp"}  # companions, not standalone media

# ── source-class heuristics ─────────────────────────────────────────────────
_RE_SONY = re.compile(r"^C\d{4,}$", re.I)                # Sony C0001 / C00014.MP4
_RE_DJI = re.compile(r"^DJI_\d+", re.I)                  # DJI drone
_RE_GOPRO = re.compile(r"^(G[XHOP]\d{6}|GOPR\d+|GP\d+)", re.I)
# search ANYWHERE in the name: the Телефоныч dump prefixes dates, e.g.
# "2023_02_11_15_08_IMG_9461.MOV" — IMG_ is not at the start.
_RE_IPHONE = re.compile(r"(^|[_\- ])(IMG|MVIMG|IMG_E)[_-]?\d", re.I)
_RE_ANDROID = re.compile(r"(^|[_\- ])(VID_\d{6,}|PXL_\d{8}|VID-\d)", re.I)
_RE_WHATSAPP = re.compile(r"whatsapp", re.I)
_RE_SCREEN = re.compile(r"(screen[\s_-]?record|screen[\s_-]?capture|screencast|"
                        r"\bobs\b|movavi|бандикам|запись экрана)", re.I)
_CAMERA_MAKES = ("sony", "dji", "gopro", "canon", "nikon", "panasonic",
                 "fujifilm", "olympus", "blackmagic")
_PHONE_MAKES = ("apple", "samsung", "google", "xiaomi", "huawei", "oneplus",
                "motorola", "lge", "htc")
_OLD_EXT = {".3gp", ".avi", ".wmv", ".mpg", ".mpeg", ".flv", ".mod"}


def ftype(ext: str) -> str:
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    return "other"


def res_tier(h: int | None) -> str:
    if not h:
        return ""
    if h >= 4320:
        return "8K"
    if h >= 2160:
        return "4K"
    if h >= 1440:
        return "QHD"
    if h >= 1080:
        return "FHD"
    if h >= 720:
        return "HD"
    return "SD"


def source_class(name: str, ext: str, clue: str, camera: str, w, h, year) -> str:
    """Best-effort provenance from filename + folder clue + camera tag + ext."""
    stem = os.path.splitext(name)[0]
    cam = (camera or "").lower()
    cl = (clue or "").lower()
    # strongest signals first
    if _RE_DJI.match(stem) or "dji" in cam or "dji" in cl:
        return "drone"
    if (_RE_SONY.match(stem) or _RE_GOPRO.match(stem) or ext in {".mts", ".m2ts", ".lrv"}
            or any(m in cam for m in _CAMERA_MAKES)
            or any(t in cl for t in ("gopro", "100media", "100gopro", "fdr-ax",
                                     "sony", "dcim/1", "head cam", "headcam"))):
        return "camera"
    if _RE_SCREEN.search(name) or _RE_SCREEN.search(cl):
        return "screen"
    if (_RE_IPHONE.search(name) or _RE_ANDROID.search(name) or _RE_WHATSAPP.search(name)
            or ext in {".heic", ".heif"} or any(m in cam for m in _PHONE_MAKES)
            or any(t in cl for t in ("iphone", "telefon", "телефон", "livephoto",
                                     "android", "mobile", "мобильник"))):
        return "phone"
    if ext in _OLD_EXT or (year and year < 2014):
        return "old"
    return "unknown"


# ── ffprobe (video) ─────────────────────────────────────────────────────────
def _fps(rate: str) -> float:
    try:
        n, d = rate.split("/")
        d = float(d)
        return round(float(n) / d, 3) if d else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def probe_video(path: Path) -> dict:
    """ffprobe one video → metadata dict (probe_ok False on any failure)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, timeout=60, text=True, encoding="utf-8",
            errors="replace",
        )
        data = json.loads(out.stdout or "{}")
    except Exception:  # noqa: BLE001 — timeout, corrupt file, ffprobe missing
        return {"probe_ok": False}
    streams = data.get("streams", [])
    fmt = data.get("format", {})
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    tags = {k.lower(): val for k, val in {**fmt.get("tags", {}),
                                          **v.get("tags", {})}.items()}
    created = (tags.get("creation_time") or tags.get("date")
               or tags.get("com.apple.quicktime.creationdate") or "")
    camera = (tags.get("com.apple.quicktime.model") or tags.get("model")
              or (f"{tags.get('com.apple.quicktime.make','')} "
                  f"{tags.get('com.apple.quicktime.model','')}".strip())
              or tags.get("make") or "")
    w = v.get("width") or 0
    h = v.get("height") or 0
    # rotation can swap display dimensions (portrait phone video)
    rot = 0
    try:
        rot = abs(int(v.get("tags", {}).get("rotate", 0)
                      or next((sd.get("rotation", 0)
                               for sd in v.get("side_data_list", [])
                               if "rotation" in sd), 0)))
    except Exception:  # noqa: BLE001
        rot = 0
    if rot in (90, 270):
        w, h = h, w
    dur = 0.0
    try:
        dur = round(float(fmt.get("duration") or v.get("duration") or 0), 2)
    except Exception:  # noqa: BLE001
        dur = 0.0
    vbr = 0
    try:
        vbr = int(fmt.get("bit_rate") or v.get("bit_rate") or 0)
    except Exception:  # noqa: BLE001
        vbr = 0
    return {
        "probe_ok": True, "w": int(w), "h": int(h),
        "codec": v.get("codec_name", ""),
        "container": (fmt.get("format_name", "").split(",")[0] or ""),
        "dur": dur, "fps": _fps(v.get("avg_frame_rate", "0/0")),
        "vbitrate": vbr, "created": str(created)[:25], "camera": camera.strip(),
        "gps": _gps_from_tags(tags),
    }


def _gps_from_tags(tags: dict):
    """Apple QuickTime stores GPS as com.apple.quicktime.location.ISO6709
    (e.g. '+37.7749-122.4194+010.000/'). Returns [lat, lon] or None."""
    loc = tags.get("com.apple.quicktime.location.iso6709") or tags.get("location")
    if not loc:
        return None
    m = re.match(r"([+-]\d+\.\d+)([+-]\d+\.\d+)", loc)
    if m:
        try:
            return [float(m.group(1)), float(m.group(2))]
        except Exception:  # noqa: BLE001
            return None
    return None


# ── exiftool (images, batched) ──────────────────────────────────────────────
def probe_images(paths: list[Path]) -> dict:
    """Run exiftool once over a batch of images → {abs_path: meta}."""
    if not paths:
        return {}
    try:
        proc = subprocess.run(
            ["exiftool", "-j", "-n", "-q", "-fast2",
             "-DateTimeOriginal", "-CreateDate", "-GPSLatitude", "-GPSLongitude",
             "-Make", "-Model", "-ImageWidth", "-ImageHeight", "-SourceFile",
             *[str(p) for p in paths]],
            capture_output=True, timeout=300, text=True, encoding="utf-8",
            errors="replace",
        )
        rows = json.loads(proc.stdout or "[]")
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for r in rows:
        sf = os.path.normcase(os.path.abspath(r.get("SourceFile", "")))
        make = str(r.get("Make", "") or "")
        model = str(r.get("Model", "") or "")
        lat, lon = r.get("GPSLatitude"), r.get("GPSLongitude")
        out[sf] = {
            "probe_ok": True,
            "w": int(r.get("ImageWidth") or 0), "h": int(r.get("ImageHeight") or 0),
            "created": str(r.get("DateTimeOriginal") or r.get("CreateDate") or "")[:25],
            "camera": f"{make} {model}".strip(),
            "gps": ([float(lat), float(lon)] if lat is not None and lon is not None
                    else None),
        }
    return out


# ── walk + orchestrate ──────────────────────────────────────────────────────
def _iter_media(root: Path, limit: int | None):
    n = 0
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None,
                                                followlinks=False):
        dirnames[:] = [d for d in dirnames
                       if d.lower() not in SKIP_DIRS and not d.startswith(".mediaexplorer")]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in IMAGE_EXT and ext not in VIDEO_EXT and ext not in AUDIO_EXT:
                continue
            p = Path(dirpath) / fn
            yield p, ext
            n += 1
            if limit and n >= limit:
                return


def _load_done(meta_path: Path) -> set[str]:
    done = set()
    if meta_path.exists():
        for line in meta_path.open(encoding="utf-8", errors="replace"):
            try:
                r = json.loads(line)
                if r.get("rel"):
                    done.add(r["rel"])
            except Exception:  # noqa: BLE001
                pass
    return done


def probe_folder(root: Path, workers: int = 12, limit: int | None = None,
                 quiet: bool = False) -> dict:
    root = Path(root).resolve()
    side = root / ".mediaexplorer"
    side.mkdir(parents=True, exist_ok=True)
    meta_path = side / "meta.jsonl"
    done = _load_done(meta_path)

    def log(*a):
        if not quiet:
            print(*a, flush=True)

    log(f"[probe] scanning {root}  (already done: {len(done)})")
    videos, images, audios = [], [], []
    for p, ext in _iter_media(root, limit):
        rel = p.relative_to(root).as_posix()
        if rel in done:
            continue
        if ext in VIDEO_EXT:
            videos.append((p, rel, ext))
        elif ext in AUDIO_EXT:
            audios.append((p, rel, ext))
        else:
            images.append((p, rel, ext))
    log(f"[probe] to do: {len(videos)} videos, {len(images)} images, {len(audios)} audio")

    lock = threading.Lock()
    out = meta_path.open("a", encoding="utf-8")
    counts = {"video": 0, "image": 0, "fail": 0}
    t0 = time.time()

    def emit(rec: dict):
        with lock:
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if not rec.get("probe_ok"):
                counts["fail"] += 1
            counts[rec["type"]] = counts.get(rec["type"], 0) + 1
            n = counts["video"] + counts["image"]
            if n % 500 == 0:
                rate = n / max(time.time() - t0, 0.1)
                log(f"  [probe] {n} done  ({rate:.0f}/s, fail {counts['fail']})")

    def base_rec(p: Path, rel: str, ext: str, typ: str) -> dict:
        try:
            st = p.stat()
            size, mtime = st.st_size, round(st.st_mtime, 0)
        except Exception:  # noqa: BLE001
            size, mtime = 0, 0
        return {"rel": rel, "abs": str(p), "name": p.name, "ext": ext,
                "type": typ, "size": size, "mtime": mtime,
                "clue": os.path.dirname(rel)}

    # videos — thread pool of ffprobe
    def do_video(item):
        p, rel, ext = item
        rec = base_rec(p, rel, ext, "video")
        rec.update(probe_video(p))
        yr = _year(rec.get("created"), rec.get("mtime"))
        rec["res_tier"] = res_tier(rec.get("h"))
        rec["source_class"] = source_class(rec["name"], ext, rec["clue"],
                                            rec.get("camera", ""),
                                            rec.get("w"), rec.get("h"), yr)
        rec["year"] = yr
        return rec

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for fut in as_completed(ex.submit(do_video, it) for it in videos):
            try:
                emit(fut.result())
            except Exception:  # noqa: BLE001
                counts["fail"] += 1

    # images — exiftool in batches of 400
    BATCH = 400
    for i in range(0, len(images), BATCH):
        chunk = images[i:i + BATCH]
        meta = probe_images([p for p, _, _ in chunk])
        for p, rel, ext in chunk:
            rec = base_rec(p, rel, ext, "image")
            m = meta.get(os.path.normcase(os.path.abspath(str(p))))
            if m:
                rec.update(m)
            else:
                rec["probe_ok"] = False
            yr = _year(rec.get("created"), rec.get("mtime"))
            rec["res_tier"] = res_tier(rec.get("h"))
            rec["source_class"] = source_class(rec["name"], ext, rec["clue"],
                                               rec.get("camera", ""),
                                               rec.get("w"), rec.get("h"), yr)
            rec["year"] = yr
            emit(rec)

    # audio — indexed for the manifest + exact-dedup (size/hash); no ffprobe
    for p, rel, ext in audios:
        rec = base_rec(p, rel, ext, "audio")
        rec["probe_ok"] = True
        rec["res_tier"] = ""
        rec["source_class"] = "audio"
        rec["year"] = _year(None, rec.get("mtime"))
        emit(rec)

    out.close()
    dt = time.time() - t0
    log(f"[probe] done: {counts} in {dt:.0f}s -> {meta_path}")
    return counts


def _year(created: str, mtime) -> int | None:
    if created:
        m = re.search(r"(19|20)\d{2}", str(created))
        if m:
            return int(m.group(0))
    if mtime:
        try:
            return time.localtime(mtime).tm_year
        except Exception:  # noqa: BLE001
            return None
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description="Probe media metadata → meta.jsonl")
    ap.add_argument("folder")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    try:  # console may be cp1251 on Windows; media paths/logs can be non-ASCII
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    root = Path(a.folder)
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2
    probe_folder(root, workers=a.workers, limit=a.limit, quiet=a.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
