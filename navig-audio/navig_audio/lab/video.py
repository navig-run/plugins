"""Dialogue cleanup inside a video: clean the voice, put it back in sync.

1. extract the first audio stream at its own rate and channel count (lossless WAV);
2. run Audio Enhance on it as speech (default preset: Voice Restoration) — the same
   measure → plan → render → VERIFY path, so a cleanup that shifts timing, drops words
   or invents sound is rejected and no video is written;
3. mux: the video stream (and subtitles / other audio) copied bit-for-bit, the cleaned
   audio re-encoded to AAC (or PCM for MOV/MKV when asked), duration checked against
   the original so the picture and the voice still line up.

The original video is never written; the result is ``<name>.cleaned.<ext>``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from navig_audio.enhance.engine import EnhanceError, enhance, output_name, sha256

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".m4v", ".webm", ".avi"}
SYNC_TOLERANCE_S = 0.02  # 20 ms: well under what a viewer notices (~45 ms lip-sync)


class VideoCleanError(RuntimeError):
    """Not a video with sound, cleanup rejected, or the mux failed."""


@dataclass
class VideoCleanResult:
    source: str
    output: str | None
    verdict: str  # pass · warn · clean (nothing to fix) · fail
    audio_stream: dict[str, Any]
    enhance: dict[str, Any]
    sync: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _exe(name: str) -> str:
    exe = shutil.which(name)
    if not exe:
        raise VideoCleanError(f"{name} is not on PATH")
    return exe


def _probe(path: Path) -> dict[str, Any]:
    proc = subprocess.run(  # noqa: S603
        [_exe("ffprobe"), "-v", "error", "-show_entries",
         "stream=index,codec_type,codec_name,sample_rate,channels:format=duration", "-of", "json", str(path)],
        capture_output=True, timeout=120, check=False,
    )
    if proc.returncode != 0:
        raise VideoCleanError(f"{path.name} is not a readable video")
    return json.loads(proc.stdout.decode("utf-8", "replace") or "{}")


def _run(args: list[str], what: str) -> None:
    proc = subprocess.run([_exe("ffmpeg"), "-hide_banner", "-nostdin", "-y", "-v", "error", *args],  # noqa: S603
                          capture_output=True, timeout=3600, check=False)
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise VideoCleanError(f"ffmpeg failed on {what}: {tail[-1] if tail else 'no output'}")


def _audio_duration(path: Path) -> float:
    info = _probe(path)
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    if a is None:
        return 0.0
    proc = subprocess.run(  # noqa: S603
        [_exe("ffprobe"), "-v", "error", "-select_streams", f"{a['index']}", "-show_entries", "stream=duration",
         "-of", "csv=p=0", str(path)], capture_output=True, timeout=60, check=False)
    try:
        return float(proc.stdout.decode().strip().splitlines()[0])
    except (ValueError, IndexError):
        return float(info.get("format", {}).get("duration") or 0.0)


def clean_video(src: Path | str, *, preset_id: str = "audio-enhance/voice-restoration",
                params: dict[str, Any] | None = None, out_dir: Path | str | None = None,
                pcm: bool = False, progress=None) -> VideoCleanResult:
    from navig_audio.enhance import presets as presets_mod

    path = Path(src).expanduser()
    if path.suffix.lower() not in VIDEO_EXTS:
        raise VideoCleanError(f"{path.name}: not a video NAVIG cleans ({', '.join(sorted(VIDEO_EXTS))})")
    info = _probe(path)
    streams = info.get("streams", [])
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if audio is None:
        raise VideoCleanError(f"{path.name} has no sound to clean")
    if not any(s.get("codec_type") == "video" for s in streams):
        raise VideoCleanError(f"{path.name} has no picture — use `navig audio enhance` for audio files")
    digest = sha256(path)
    try:
        settings = presets_mod.load(preset_id)
    except KeyError:
        raise VideoCleanError(f"no enhancement preset {preset_id!r}") from None
    settings.update(params or {})
    settings.setdefault("category", "speech")
    # Trimming would shift every later word against the picture — never in a video.
    settings["trim_silence"] = False
    dest = Path(out_dir).expanduser() if out_dir else path.parent
    dest.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="navig-video-clean-") as tmp:
        work = Path(tmp)
        raw = work / f"{path.stem}.wav"
        _run(["-i", str(path), "-map", f"0:{audio['index']}", "-vn", "-c:a", "pcm_s24le", str(raw)],
             "extracting the sound")
        try:
            res = enhance(raw, mode=str(settings.pop("mode", "studio")), params=settings,
                          out_dir=work / "enh", formats=["wav"], preset_id=preset_id, progress=progress)
        except EnhanceError as exc:
            raise VideoCleanError(str(exc)) from exc
        summary = {"verdict": res.verdict, "stages": [s.name for s in res.plan.stages],
                   "summary": res.verification.summary if res.verification else ""}
        if res.verdict == "clean":
            return VideoCleanResult(str(path), None, "clean", audio, summary)
        if res.verdict == "fail" or not res.outputs:
            return VideoCleanResult(str(path), None, "fail", audio, summary)
        cleaned = Path(res.outputs[0])
        out = output_name(path, dest, path.suffix.lower(), "cleaned")
        codec = ["-c:a", "pcm_s24le"] if pcm and path.suffix.lower() in (".mov", ".mkv") else ["-c:a", "aac", "-b:a", "256k"]
        try:
            _run(["-i", str(path), "-i", str(cleaned),
                  "-map", "0:v", "-map", "1:a:0", "-map", "0:s?", "-c:v", "copy", "-c:s", "copy", *codec,
                  "-map_metadata", "0", str(out)], "putting the sound back")
            before, after = _audio_duration(path), _audio_duration(out)
            drift = abs(after - before)
            sync = {"original_audio_s": round(before, 3), "cleaned_audio_s": round(after, 3),
                    "drift_s": round(drift, 4), "ok": drift <= SYNC_TOLERANCE_S + 0.03}
            # AAC adds encoder priming (~21 ms at 48 kHz) that players compensate; the
            # alignment itself was already verified sample-accurate on the WAV above.
            if not sync["ok"]:
                out.unlink(missing_ok=True)
                raise VideoCleanError(f"the cleaned sound came out {drift * 1000:.0f} ms off the original — not saved")
        except VideoCleanError:
            out.unlink(missing_ok=True)
            raise
    if sha256(path) != digest:
        raise VideoCleanError("the original video changed during cleanup — output not trusted")
    return VideoCleanResult(str(path), str(out), res.verdict, audio, summary, sync)
