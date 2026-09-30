"""Phase 7: merge an episode's source clips into one RAW file (lossless).

Camera clips (Sony/GoPro) often carry per-file timestamp resets, so a plain
``concat`` demuxer with ``-c copy`` silently keeps only the first segment. This
uses the robust method: remux each clip to MPEG-TS (lossless ``-c copy`` with the
right Annex-B bitstream filter), concat the TS streams, then remux back to MP4 —
no re-encode, full quality.

Safety:
- Unreadable/corrupt clips are SKIPPED with an explicit warning (never silently).
- Mixed codecs/resolutions stop the run (a re-encode would be a deliberate choice).
- The output duration is checked against the sum of inputs; a mismatch is reported.
- Non-destructive: source clips are left in place.

    python merge.py "<episode folder>" [--out <file.mp4>]
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

VID = {".mp4", ".mov", ".mts", ".m2ts", ".avi", ".m4v"}
# codec -> Annex-B bitstream filter for the MP4->TS remux
_BSF = {"h264": "h264_mp4toannexb", "hevc": "hevc_mp4toannexb"}


def _natkey(p: Path):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", p.name)]


def _probe(p: Path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-show_format", str(p)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60)
        d = json.loads(out.stdout or "{}")
        v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), {})
        a = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), {})
        return {"vcodec": v.get("codec_name"), "w": v.get("width"), "h": v.get("height"),
                "acodec": a.get("codec_name"),
                "dur": float(d.get("format", {}).get("duration") or 0)}
    except Exception:  # noqa: BLE001
        return {"vcodec": None, "w": None, "h": None, "acodec": None, "dur": 0.0}


def merge_episode(folder: Path, out: Path | None = None, quiet: bool = False) -> dict:
    def log(*a):
        if not quiet:
            print(*a, flush=True)

    folder = Path(folder)
    clips = sorted([p for p in folder.iterdir() if p.suffix.lower() in VID], key=_natkey)
    if not clips:
        log("[merge] no clips found")
        return {"ok": False, "reason": "no-clips"}
    sigs = [(c, _probe(c)) for c in clips]
    good = [(c, s) for c, s in sigs if s["vcodec"]]
    bad = [c for c, s in sigs if not s["vcodec"]]
    if bad:
        log(f"[merge] SKIPPING {len(bad)} unreadable/corrupt clip(s) (pre-existing "
            f"damage): {[c.name for c in bad]}")
    if not good:
        return {"ok": False, "reason": "no-readable-clips", "skipped": len(bad)}
    formats = {(s["vcodec"], s["w"], s["h"]) for _, s in good}
    total = sum(s["dur"] for _, s in good)
    vcodec = good[0][1]["vcodec"]
    log(f"[merge] {len(good)} readable clips · {total/60:.1f} min · formats {formats}")
    if len(formats) > 1:
        log("[merge] MIXED formats — lossless concat unsafe; re-encode needed "
            "(not done automatically). Stopping.")
        return {"ok": False, "reason": "mixed-formats", "formats": list(formats)}
    bsf = _BSF.get(vcodec)
    if not bsf:
        log(f"[merge] codec {vcodec} has no known Annex-B filter; re-encode needed. Stopping.")
        return {"ok": False, "reason": f"unsupported-codec:{vcodec}"}

    out = Path(out) if out else folder.parent / "_merged" / f"{folder.name}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="ep_merge_", dir=str(out.parent)))
    try:
        ts_files = []
        for i, (c, _) in enumerate(good):
            ts = tmp / f"{i:04d}.ts"
            r = subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-i", str(c),
                 "-c", "copy", "-bsf:v", bsf, "-f", "mpegts", str(ts)],
                capture_output=True, text=True, encoding="utf-8", errors="replace")
            if r.returncode != 0 or not ts.exists():
                log(f"[merge] TS remux failed on {c.name}: {r.stderr[:200]}")
                return {"ok": False, "reason": "ts-remux-failed", "clip": c.name}
            ts_files.append(ts)
            if (i + 1) % 20 == 0:
                log(f"  [merge] remuxed {i+1}/{len(good)} to TS")
        concat = "concat:" + "|".join(str(t) for t in ts_files)
        aud = ["-bsf:a", "aac_adtstoasc"] if good[0][1]["acodec"] == "aac" else []
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", concat, "-c", "copy", *aud, str(out)],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            log(f"[merge] final concat failed: {r.stderr[:300]}")
            return {"ok": False, "reason": "concat-failed"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    outdur = _probe(out)["dur"]
    sz = out.stat().st_size / 1024**3
    ok = abs(outdur - total) < 3.0
    log(f"[merge] {'DONE' if ok else 'DURATION MISMATCH'}: {out} · {sz:.2f} GB · "
        f"{outdur/60:.1f} min (expected {total/60:.1f})")
    return {"ok": ok, "out": str(out), "gb": round(sz, 2),
            "minutes": round(outdur / 60, 1), "clips": len(good), "skipped": len(bad)}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Merge an episode's clips into one RAW file")
    ap.add_argument("folder")
    ap.add_argument("--out", default=None)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    res = merge_episode(Path(a.folder), Path(a.out) if a.out else None, quiet=a.quiet)
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
