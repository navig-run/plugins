"""Ambience beds: loopable room tones and atmospheres at any length.

Source, one of:
* ``generate`` — ElevenLabs sound generation from your description (≤ 22 s per take,
  billed; needs ``allow_cloud``). Real-sounding café, street, forest…
* ``synth``    — local shaped noise for the beds noise CAN make honestly: rain, wind,
  server room, room tone, pink / brown / white. Free, offline, endless.

Then a seamless loop: the tail is equal-power crossfaded over the head, so the file's
end flows into its start. The seam is MEASURED (level step and spectral jump at the
join vs the bed's own frame-to-frame variation) — "seamless" is a checked claim.
Outputs: ``<name>.loop.wav`` and ``<name>.<N>s.wav`` (the loop rendered to length).
"""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

SR = 48000
GEN_MAX_S = 22.0
SYNTH = {
    # name → ffmpeg lavfi source + shaping (all noise-based: no fake "events")
    "rain": "anoisesrc=color=pink:amplitude=0.35:seed={seed},highpass=f=300,lowpass=f=9000,"
            "tremolo=f=0.13:d=0.25,equalizer=f=3000:width_type=o:w=2:g=3",
    "wind": "anoisesrc=color=brown:amplitude=0.6:seed={seed},lowpass=f=900,tremolo=f=0.1:d=0.35,tremolo=f=0.25:d=0.15",
    "server-room": "anoisesrc=color=pink:amplitude=0.2:seed={seed},bandpass=f=1200:width_type=o:w=3,"
                   "volume=1.2[n];sine=f=120:sample_rate=48000,volume=0.02[h];[n][h]amix=inputs=2:normalize=0",
    "room-tone": "anoisesrc=color=pink:amplitude=0.05:seed={seed},lowpass=f=4000,highpass=f=60",
    "pink": "anoisesrc=color=pink:amplitude=0.3:seed={seed}",
    "brown": "anoisesrc=color=brown:amplitude=0.5:seed={seed}",
    "white": "anoisesrc=color=white:amplitude=0.15:seed={seed}",
}


class AmbienceError(RuntimeError):
    """Bad request, no consent for generation, provider or ffmpeg failure."""


@dataclass
class AmbienceResult:
    loop: str
    render: str
    seconds: float
    loop_seconds: float
    source: str  # generated · synth
    prompt: str
    seam: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _ff(args: list[str], what: str, stdin: bytes | None = None) -> bytes:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise AmbienceError("ffmpeg is not on PATH")
    base = [exe, "-hide_banner", "-y", "-v", "error"] + ([] if stdin is not None else ["-nostdin"])
    proc = subprocess.run([*base, *args], input=stdin, capture_output=True, timeout=900, check=False)  # noqa: S603
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise AmbienceError(f"ffmpeg failed on {what}: {tail[-1] if tail else 'no output'}")
    return proc.stdout


def _decode(path: Path) -> np.ndarray:
    raw = _ff(["-i", str(path), "-f", "f32le", "-ac", "2", "-ar", str(SR), "-"], "decoding")
    x = np.frombuffer(raw, dtype="<f4")
    return x[: x.size - x.size % 2].reshape(-1, 2).T.copy()


def _write(x: np.ndarray, dst: Path) -> None:
    pcm = np.clip(x, -1, 1).T.astype("<f4").tobytes()
    _ff(["-f", "f32le", "-ar", str(SR), "-ac", str(x.shape[0]), "-i", "-", "-c:a", "pcm_s24le", str(dst)],
        "writing", stdin=pcm)


def make_loop(x: np.ndarray, xfade_s: float) -> np.ndarray:
    """Equal-power crossfade of the last ``xfade_s`` over the first — end flows into start."""
    n = int(xfade_s * SR)
    if x.shape[1] < 3 * n:
        raise AmbienceError("the source is too short to loop with that crossfade")
    t = np.linspace(0, np.pi / 2, n)
    fade_in, fade_out = np.sin(t) ** 2, np.cos(t) ** 2
    out = x[:, :-n].copy()
    out[:, :n] = x[:, :n] * fade_in + x[:, -n:] * fade_out
    return out


def seam_report(loop: np.ndarray) -> dict[str, Any]:
    """Level step and spectral change across the join, vs the bed's own variation."""
    hop = int(0.05 * SR)
    joined = np.concatenate([loop[:, -4 * hop:], loop[:, :4 * hop]], axis=1).mean(axis=0)
    body = loop.mean(axis=0)

    def frames(sig: np.ndarray) -> np.ndarray:
        k = sig.size // hop
        f = sig[: k * hop].reshape(k, hop)
        return np.abs(np.fft.rfft(f * np.hanning(hop), axis=1)) + 1e-9

    jb, bb = frames(joined), frames(body)
    jump = float(np.mean(np.abs(np.log(jb[4]) - np.log(jb[3]))))
    steps = np.mean(np.abs(np.diff(np.log(bb), axis=0)), axis=1)
    typical = float(np.percentile(steps, 90)) if steps.size else jump
    lvl = lambda s: 20 * np.log10(np.sqrt(np.mean(s**2)) + 1e-12)  # noqa: E731
    step_db = float(abs(lvl(joined[3 * hop:4 * hop]) - lvl(joined[4 * hop:5 * hop])))
    smooth = jump <= typical * 1.2 and step_db <= 1.5
    return {"spectral_jump": round(jump, 3), "typical_frame_change": round(typical, 3),
            "level_step_db": round(step_db, 2), "smooth": bool(smooth)}


def ambience(prompt: str, *, seconds: float, out_dir: Path | str, source: str = "synth",
             loop_seconds: float = 20.0, allow_cloud: bool = False, seed: int = 7, provider=None) -> AmbienceResult:
    if not 5 <= seconds <= 3 * 3600:
        raise AmbienceError("length must be between 5 s and 3 h")
    dest = Path(out_dir)
    dest.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^\w\-]+", "-", prompt.strip().lower()).strip("-")[:40] or "ambience"
    with tempfile.TemporaryDirectory(prefix="navig-ambience-") as tmp:
        work = Path(tmp)
        raw = work / "raw.wav"
        if source == "synth":
            key = prompt.strip().lower().replace(" ", "-")
            if key not in SYNTH:
                raise AmbienceError(f"local beds: {', '.join(SYNTH)} — describe anything else with source=generate")
            graph = SYNTH[key].format(seed=int(seed))
            src_s = max(loop_seconds + 4, 12.0)
            if ";" in graph or "[" in graph:
                _ff(["-filter_complex", graph + ",atrim=duration=" + f"{src_s:.2f}", "-ar", str(SR), "-ac", "2", str(raw)], "synthesising")
            else:
                _ff(["-f", "lavfi", "-i", graph, "-t", f"{src_s:.2f}", "-ar", str(SR), "-ac", "2", str(raw)], "synthesising")
            text = f"local {key} bed (shaped noise)"
        elif source == "generate":
            if not allow_cloud:
                raise AmbienceError("generating ambience bills ElevenLabs — allow cloud processing for this run")
            from navig_audio.regenerate.core import ElevenLabsProvider

            prov = provider or ElevenLabsProvider()
            text = (f"seamless continuous ambience bed: {prompt.strip()}. steady, even, no sudden events, "
                    "no music, no speech up front")
            data = asyncio.run(prov.sfx(text, GEN_MAX_S))
            if not data:
                raise AmbienceError("the provider returned no audio")
            (work / "gen.bin").write_bytes(data)
            _ff(["-i", str(work / "gen.bin"), "-ar", str(SR), "-ac", "2", str(raw)], "converting the bed")
        else:
            raise AmbienceError("source must be synth or generate")
        x = _decode(raw)
        xfade = min(3.0, max(0.5, x.shape[1] / SR / 6))
        loop = make_loop(x, xfade)
        from navig_audio.mastering.master import unique_path

        loop_path = unique_path(dest / f"{name}.loop.wav")
        _write(loop, loop_path)
        reps = int(np.ceil(seconds * SR / loop.shape[1]))
        long = np.tile(loop, reps)[:, : int(seconds * SR)]
        fade = int(min(2.0, seconds / 10) * SR)
        ramp = np.linspace(0, 1, fade)
        long[:, :fade] *= ramp
        long[:, -fade:] *= ramp[::-1]
        render = unique_path(dest / f"{name}.{int(seconds)}s.wav")
        _write(long, render)
        return AmbienceResult(str(loop_path), str(render), float(seconds), round(loop.shape[1] / SR, 3),
                              source if source == "synth" else "generated", text, seam_report(loop))
