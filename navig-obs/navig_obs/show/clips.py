"""Pigeon moments → vertical clips (``navig obs clips``).

A session written by :mod:`navig_obs.show.moments` holds the moments
(``markers.jsonl``, labels ``pigeon-*``) and his lines (``pigeon.jsonl``). This
module turns them into short 9:16 clips:

1. **plan** (pure): each moment becomes a window — a beat before it, as long
   after as that kind of moment needs (hacker mode runs ~13 s with its line);
   windows that touch are merged; every line inside a window becomes a caption;
2. **render**: cut the window from the recording (frame-accurate re-encode),
   crop a 9:16 frame around the pigeon (``focus`` = his x as a fraction of the
   canvas; a portrait recording is only scaled), loudness-master the audio and
   burn the captions — the shared media engine (navig-media-core) does each step.

Outputs land in ``<session>/clips/`` with ``clips.json`` beside them; an existing
clip is kept unless ``force``. The recording is read, never written.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PRE_S = 1.5
MERGE_GAP_S = 1.0
MAX_CLIP_S = 30.0
# How long after the moment the clip keeps rolling (its animation + his line).
POST_S: dict[str, float] = {
    "hacker": 13.0, "celebrate": 6.0, "windfall": 9.0, "cuckoo": 10.0,
    "mission": 7.0, "passport": 7.0, "join": 6.0, "warning": 6.0, "dance": 5.0,
    "moonwalk": 6.0, "malfunction": 5.0, "screenpeck": 5.0, "anger": 8.0,
    "uptime": 9.0, "lag": 9.0, "live": 9.0,
}
DEFAULT_POST_S = 6.0
VIDEO_SUFFIXES = (".mp4", ".mkv", ".mov", ".ts", ".flv", ".m4v")


@dataclass
class Caption:
    t0: float
    t1: float
    text: str


@dataclass
class Clip:
    t0: float
    t1: float
    kinds: list[str]
    captions: list[Caption] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "+".join(dict.fromkeys(self.kinds))[:60]

    def to_json(self) -> dict[str, Any]:
        return {
            "t0": round(self.t0, 3), "t1": round(self.t1, 3), "kinds": self.kinds,
            "captions": [{"t0": round(c.t0, 3), "t1": round(c.t1, 3), "text": c.text}
                         for c in self.captions],
        }


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            row = json.loads(ln)
        except ValueError:
            continue  # one torn line never sinks the session
        if isinstance(row, dict):
            out.append(row)
    return out


def plan(
    markers: list[dict[str, Any]], lines: list[dict[str, Any]], duration: float | None = None,
) -> list[Clip]:
    """The clips a session's moments make (pure: no files, no ffmpeg)."""
    moments = sorted(
        (float(m["tOffset"]), str(m["label"])[len("pigeon-"):])
        for m in markers
        if str(m.get("label", "")).startswith("pigeon-") and "tOffset" in m
    )
    clips: list[Clip] = []
    for t, kind in moments:
        t0 = max(0.0, t - PRE_S)
        t1 = t + POST_S.get(kind, DEFAULT_POST_S)
        if duration is not None:
            t1 = min(t1, duration)
        if t1 - t0 < 1.0:
            continue
        last = clips[-1] if clips else None
        if last and t0 <= last.t1 + MERGE_GAP_S and max(t1, last.t1) - last.t0 <= MAX_CLIP_S:
            last.t1 = max(last.t1, t1)
            last.kinds.append(kind)
        else:
            clips.append(Clip(t0, t1, [kind]))
    for c in clips:
        for ln in sorted(lines, key=lambda r: float(r.get("tOffset", 0))):
            try:
                s = float(ln["tOffset"])
                e = s + max(0.5, float(ln.get("ms", 3000)) / 1000.0)
            except (KeyError, TypeError, ValueError):
                continue
            if e <= c.t0 or s >= c.t1:
                continue
            text = " ".join(str(ln.get("text", "")).split())
            if text:
                c.captions.append(Caption(max(0.0, s - c.t0), min(c.t1, e) - c.t0, text))
    return clips


def srt(captions: list[Caption]) -> str:
    def ts(x: float) -> str:
        ms = int(round(x * 1000))
        h, ms = divmod(ms, 3_600_000)
        m, ms = divmod(ms, 60_000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    blocks = []
    for i, c in enumerate(captions, 1):
        blocks.append(f"{i}\n{ts(c.t0)} --> {ts(c.t1)}\n{c.text}\n")
    return "\n".join(blocks)


LAYOUTS = ("square", "tall")
SQUARE_Y = 0.10  # the square's top in the 9:16 frame: the lower band is the captions'
SQUARE_SIDE = 0.8  # the square, as a share of the canvas height: him, bigger
FEET = 0.85  # his feet line, 0..1 of the canvas height (scenes.json pigeon y=0.15)


def _x(width: int, cw: int, focus: float) -> int:
    x = int(round(focus * width - cw / 2))
    return max(0, min(width - cw, x)) // 2 * 2


def crop_filter(
    width: int, height: int, focus: float, layout: str = "square",
    out_w: int = 1080, out_h: int = 1920, feet: float = FEET,
) -> str:
    """A filter_complex from ``[0:v]`` to ``[v]``: the 9:16 frame around him.

    * a portrait recording is scaled to cover;
    * ``tall``: a full-height 9:16 slice centred on ``focus`` (608 px of a
      1080p frame — narrower than his bubble and the hacker terminal);
    * ``square`` (default): a height-sized square around him — bubble,
      terminal and bird fit — over a blurred, darkened copy of itself, with
      the lower band left for the burned captions. Never black bars.
    """
    if height >= width:
        return (f"[0:v]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
                f"crop={out_w}:{out_h},setsar=1,format=yuv420p[v]")
    if layout == "tall":
        cw = int(round(height * 9 / 16)) // 2 * 2
        return (f"[0:v]crop={cw}:{height}:{_x(width, cw, focus)}:0,"
                f"scale={out_w}:{out_h},setsar=1,format=yuv420p[v]")
    side = int(height * SQUARE_SIDE) // 2 * 2
    # The square's bottom sits just under his feet; what is above him (bubble,
    # terminal, fireworks) fills the rest.
    sy = int(round(feet * height + 0.06 * height)) - side
    sy = max(0, min(height - side, sy)) // 2 * 2
    y = int(round(out_h * SQUARE_Y)) // 2 * 2
    return (
        f"[0:v]crop={side}:{side}:{_x(width, side, focus)}:{sy},split[a][b];"
        f"[a]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,crop={out_w}:{out_h},"
        f"boxblur=24:2,eq=brightness=-0.06[bg];"
        f"[b]scale={out_w}:{out_w}[fg];"
        f"[bg][fg]overlay=0:{y},setsar=1,format=yuv420p[v]"
    )


def find_recording(session_dir: Path, record: dict[str, Any], meta: dict[str, Any]) -> Path | None:
    """The file this session recorded into: the one in session.json, else the
    video in the record directory created closest to the session's start."""
    p = record.get("recordingPath")
    if p and Path(p).is_file():
        return Path(p)
    root = meta.get("recordDirectory")
    if not root or not Path(root).is_dir():
        return None
    start = int(record.get("startEpoch") or 0) / 1000.0
    best: tuple[float, Path] | None = None
    for f in Path(root).iterdir():
        if not f.is_file() or f.suffix.lower() not in VIDEO_SUFFIXES:
            continue
        st = f.stat()
        # Windows: st_ctime is creation. The file must have been written after
        # the start and created within a minute of it.
        if st.st_mtime < start - 5:
            continue
        gap = abs(st.st_ctime - start)
        if gap <= 60 and (best is None or gap < best[0]):
            best = (gap, f)
    return best[1] if best else None


def render(
    recording: Path, clips: list[Clip], out_dir: Path, *, prefix: str, focus: float,
    force: bool = False, layout: str = "square", captions: bool = True,
) -> list[dict[str, Any]]:
    """Cut, crop, master and caption each clip. Needs navig-media-core + ffmpeg."""
    from navig_media_core.media.video_edit import (  # noqa: PLC0415 — an extra
        VideoEditError,
        burn_captions,
        excerpt,
        master,
        probe,
    )

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not on PATH")
    info = probe(recording)
    fc = crop_filter(int(info["width"]), int(info["height"]), focus, layout)
    work = out_dir / "_work"
    work.mkdir(parents=True, exist_ok=True)
    results = []
    for i, c in enumerate(clips, 1):
        dst = out_dir / f"{prefix}-{i:02d}-{c.name}.mp4"
        row = {"file": str(dst), **c.to_json()}
        if dst.is_file() and not force:
            results.append({**row, "cached": True})
            continue
        part = work / f"{i:02d}-cut.mp4"
        excerpt(recording, part, c.t0, c.t1, crf=18, maxrate="10M")
        vert = work / f"{i:02d}-vertical.mp4"
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            [ffmpeg, "-nostdin", "-y", "-i", str(part), "-filter_complex", fc,
             "-map", "[v]", "-map", "0:a?",
             "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-c:a", "copy", str(vert)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if proc.returncode != 0 or not vert.is_file():
            raise RuntimeError(f"vertical crop failed: {(proc.stderr or '').strip()[-300:]}")
        loud = work / f"{i:02d}-loud.mp4"
        try:
            master(vert, loud, i=-14.0, tp=-1.0)
        except VideoEditError:
            loud = vert  # a recording with no audio track keeps its silence
        if c.captions and captions:
            sub = work / f"{i:02d}.srt"
            sub.write_text(srt(c.captions), encoding="utf-8")
            burn_captions(loud, sub, dst)
        else:
            shutil.copy2(loud, dst)
        results.append({**row, "cached": False, "bytes": dst.stat().st_size})
    shutil.rmtree(work, ignore_errors=True)
    return results
