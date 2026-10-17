"""Score to picture: music written to the video's cuts, mixed under its dialogue.

1. **Cuts** — ffmpeg's scene detector finds the hard cuts; scenes shorter than 3 s are
   merged into their neighbour so the music doesn't change every second.
2. **Plan** — one ElevenLabs ``composition_plan`` section per scene, each exactly the
   scene's length, so the music turns where the picture turns (``respect_sections_durations``).
3. **Render once** (billed, ``allow_cloud``), then fit it to the picture: trimmed or
   padded to the exact length with a 2 s fade at the end.
4. **Mix** — when the video already has sound, the music ducks under it (sidechain
   compression); the picture is copied untouched. The music is also saved on its own.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

SCENE_THRESHOLD = 0.3
MIN_SCENE_S = 3.0
SECTION_MAX_S = 120.0
MIN_VIDEO_S = 10.0
MAX_VIDEO_S = 300.0  # the music endpoint renders at most five minutes
MUSIC_GAIN_DB = -10.0
FADE_OUT_S = 2.0


class ScoreError(RuntimeError):
    """No consent, unusable video, provider failure, or ffmpeg failure."""


class ScoreProvider(Protocol):
    async def compose(self, plan: dict[str, Any]) -> bytes: ...


class ElevenLabsScore:
    async def compose(self, plan: dict[str, Any]) -> bytes:
        from navig_generate.tools.audio_generation import AudioGenerationConfig, AudioGenerator

        gen = AudioGenerator(AudioGenerationConfig(save_locally=False))
        try:
            res = await gen.generate("score to picture", kind="music", save=False, composition_plan=plan)
            return res.audio or b""
        finally:
            await gen.close()


@dataclass
class ScoreResult:
    source: str
    cuts: list[float]
    scenes: list[tuple[float, float]]
    plan: dict[str, Any]
    output: str | None = None
    music: str | None = None
    ducked: bool = False
    sidecar: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "generated": True}


def _exe(name: str) -> str:
    exe = shutil.which(name)
    if not exe:
        raise ScoreError(f"{name} is not on PATH")
    return exe


def _ffmpeg(*args: str, timeout: int = 1800) -> None:
    proc = subprocess.run([_exe("ffmpeg"), "-hide_banner", "-nostdin", "-y", "-v", "error", *args],  # noqa: S603
                          capture_output=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ScoreError(f"ffmpeg failed: {tail[-1] if tail else 'no output'}")


def _streams(path: Path) -> tuple[float, bool, bool]:
    out = subprocess.run([_exe("ffprobe"), "-v", "error", "-show_entries", "format=duration:stream=codec_type",  # noqa: S603
                          "-of", "json", str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, check=False)
    try:
        d = json.loads(out.stdout or "{}")
        dur = float(d["format"]["duration"])
    except (KeyError, ValueError, TypeError) as exc:
        raise ScoreError(f"cannot read {path.name}") from exc
    kinds = {s.get("codec_type") for s in d.get("streams", [])}
    return dur, "video" in kinds, "audio" in kinds


def detect_cuts(path: Path, threshold: float = SCENE_THRESHOLD) -> list[float]:
    proc = subprocess.run([_exe("ffmpeg"), "-hide_banner", "-nostdin", "-i", str(path), "-map", "0:v:0",  # noqa: S603
                           "-vf", f"select='gt(scene,{threshold})',showinfo", "-f", "null", "-"],
                          capture_output=True, timeout=1800, check=False)
    text = proc.stderr.decode("utf-8", "replace")
    return sorted({round(float(m), 3) for m in re.findall(r"pts_time:([0-9.]+)", text)})


def scenes_from_cuts(cuts: list[float], duration: float, *, min_scene: float = MIN_SCENE_S) -> list[tuple[float, float]]:
    bounds = [0.0] + [c for c in cuts if 0.0 < c < duration] + [duration]
    scenes = [(a, b) for a, b in zip(bounds, bounds[1:]) if b > a]
    merged: list[list[float]] = []
    for a, b in scenes:  # a too-short scene joins the one before it
        if merged and (b - a < min_scene or merged[-1][1] - merged[-1][0] < min_scene):
            merged[-1][1] = b
        else:
            merged.append([a, b])
    if len(merged) > 1 and merged[-1][1] - merged[-1][0] < min_scene:
        last = merged.pop()
        merged[-1][1] = last[1]
    out: list[tuple[float, float]] = []
    for a, b in merged:  # a scene over the provider's section cap becomes equal parts
        parts = max(1, int(-(-(b - a) // SECTION_MAX_S)))
        step = (b - a) / parts
        out += [(round(a + i * step, 3), round(a + (i + 1) * step, 3)) for i in range(parts)]
    return out


def build_plan(scenes: list[tuple[float, float]], style: str) -> dict[str, Any]:
    styles = [s.strip() for s in re.split(r"[,;]", style) if s.strip()] or ["cinematic underscore"]
    sections = []
    for i, (a, b) in enumerate(scenes):
        local = ["establishing, sparse"] if i == 0 else ["resolving, final cadence"] if i == len(scenes) - 1 \
            else ["new motif for a new scene"]
        sections.append({"section_name": f"Scene {i + 1} ({a:.1f}–{b:.1f} s)", "positive_local_styles": local,
                         "negative_local_styles": [], "duration_ms": int(round((b - a) * 1000)), "lines": []})
    # The section lengths must add up to the picture exactly; rounding error goes on the last one.
    total_ms = int(round(scenes[-1][1] * 1000))
    sections[-1]["duration_ms"] += total_ms - sum(s["duration_ms"] for s in sections)
    return {"positive_global_styles": [*styles, "film score", "instrumental"],
            "negative_global_styles": ["vocals", "singing", "lyrics"], "sections": sections}


def score_to_picture(video: Path | str, style: str, *, allow_cloud: bool = False, out_dir: Path | str | None = None,
                     threshold: float = SCENE_THRESHOLD, duck: bool = True, provider: ScoreProvider | None = None,
                     dry_run: bool = False, progress: Callable[[str], None] | None = None) -> ScoreResult:
    path = Path(video)
    if not path.is_file():
        raise ScoreError(f"no such file: {path}")
    dur, has_video, has_audio = _streams(path)
    if not has_video:
        raise ScoreError("this is not a video — score-to-picture needs pictures to follow")
    if dur < MIN_VIDEO_S or dur > MAX_VIDEO_S:
        raise ScoreError(f"the video must be {MIN_VIDEO_S:.0f} s – {MAX_VIDEO_S / 60:.0f} min long (it is {dur:.1f} s)")
    if progress:
        progress("finding the cuts")
    cuts = detect_cuts(path, threshold)
    scenes = scenes_from_cuts(cuts, dur)
    plan = build_plan(scenes, style)
    result = ScoreResult(str(path), cuts, scenes, plan)
    if dry_run:
        return result
    if not allow_cloud:
        raise ScoreError("composing uploads only the plan (no media) to ElevenLabs but is billed — allow cloud processing")
    from navig_audio.mastering.master import unique_path

    dest = Path(out_dir) if out_dir else path.parent
    dest.mkdir(parents=True, exist_ok=True)
    if progress:
        progress(f"composing {len(scenes)} sections")
    blob = asyncio.run((provider or ElevenLabsScore()).compose(plan))
    if not blob:
        raise ScoreError("the music service returned no audio")
    with tempfile.TemporaryDirectory(prefix="navig-score-") as td:
        raw = Path(td) / "music.bin"
        raw.write_bytes(blob)
        music = unique_path(dest / f"{path.stem}.score.wav")
        fade_at = max(0.0, dur - FADE_OUT_S)
        _ffmpeg("-i", str(raw), "-af", f"apad,atrim=0:{dur:.3f},afade=t=out:st={fade_at:.3f}:d={FADE_OUT_S}",
                "-ar", "48000", "-ac", "2", "-c:a", "pcm_s24le", str(music))
        out = unique_path(dest / f"{path.stem}.scored{path.suffix}")
        if has_audio and duck:
            graph = (f"[1:a]volume={MUSIC_GAIN_DB}dB,aformat=channel_layouts=stereo[m];"
                     "[0:a:0]aformat=channel_layouts=stereo,asplit=2[d][key];"
                     "[m][key]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[duck];"
                     "[d][duck]amix=inputs=2:normalize=0:duration=first[a]")
            _ffmpeg("-i", str(path), "-i", str(music), "-filter_complex", graph, "-map", "0:v", "-map", "[a]",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "256k", "-t", f"{dur:.3f}", str(out))
            result.ducked = True
        else:
            _ffmpeg("-i", str(path), "-i", str(music), "-map", "0:v", "-map", "1:a", "-c:v", "copy",
                    "-c:a", "aac", "-b:a", "256k", "-t", f"{dur:.3f}", str(out))
            if has_audio:
                result.notes.append("the original sound was replaced (duck off)")
    result.output, result.music = str(out), str(music)
    sidecar = unique_path(dest / f"{path.stem}.score.json")
    sidecar.write_text(json.dumps({**result.to_dict(), "note": "AI-generated music (ElevenLabs composition)."},
                                  indent=2, ensure_ascii=False), encoding="utf-8")
    result.sidecar = str(sidecar)
    return result
