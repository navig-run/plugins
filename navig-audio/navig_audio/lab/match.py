"""Reference matching: make a track sit like a reference — tone and loudness, locally.

* **Tone** — both files' long-term third-octave balance is measured; the difference
  becomes a smoothed EQ curve (FIR, ffmpeg ``firequalizer``), applied at ``strength``
  (default 70 %: nudge toward the reference, don't clone it) and capped at ±6 dB per
  band, so a reference with no sub-bass cannot make the track boom or vice versa.
* **Loudness** — levelled to the reference's own integrated LUFS (true peak ≤ −1 dBTP).
* **Dynamics** — kept by default. ``match_dynamics`` adds gentle compression only when
  the track is markedly more dynamic (crest factor) than the reference.

The report says how far the tone was from the reference before and after (dB RMS over
the bands both files use), so "matched" is a measured claim.
"""

from __future__ import annotations

import math
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from navig_audio.enhance.analysis import AnalyzeError, analyze, decode

MAX_BAND_DB = 6.0


class MatchError(RuntimeError):
    """Unreadable input, a silent reference, or an ffmpeg failure."""


@dataclass
class MatchResult:
    output: str
    reference: str
    strength: float
    eq_db: dict[str, float]
    tone_distance_before_db: float
    tone_distance_after_db: float
    lufs_before: float
    lufs_after: float
    lufs_reference: float
    dynamics: str

    def to_dict(self) -> dict[str, Any]:
        return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


def _balance(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """(centres Hz, level dB relative to its own mean) — the shape, not the loudness."""
    from navig_audio.enhance.verify import _third_octave

    rep = analyze(path)
    x = decode(path, rep.sample_rate, rep.channels).mean(axis=0)
    centres, levels = _third_octave(rep.sample_rate, x)
    live = levels > levels.max() - 60
    rel = levels - (levels[live].mean() if live.any() else 0.0)
    rel[~live] = np.nan
    return centres, rel


def tone_distance(a: tuple[np.ndarray, np.ndarray], b: tuple[np.ndarray, np.ndarray]) -> float:
    n = min(a[1].size, b[1].size)
    d = a[1][:n] - b[1][:n]
    d = d[np.isfinite(d)]
    return float(np.sqrt(np.mean(d**2))) if d.size else 0.0


def eq_curve(src: tuple[np.ndarray, np.ndarray], ref: tuple[np.ndarray, np.ndarray], strength: float) -> dict[float, float]:
    n = min(src[1].size, ref[1].size)
    centres = src[0][:n]
    diff = ref[1][:n] - src[1][:n]
    ok = np.isfinite(diff)
    if not ok.any():
        return {}
    filled = np.where(ok, diff, 0.0)
    weight = ok.astype(float)
    k = np.ones(3) / 3
    smooth = np.convolve(filled, k, mode="same") / np.maximum(np.convolve(weight, k, mode="same"), 1e-9)
    gains = np.clip(smooth * strength, -MAX_BAND_DB, MAX_BAND_DB)
    return {float(c): round(float(g), 2) for c, g, o in zip(centres, gains, ok) if o and abs(g) >= 0.25}


def match_reference(src: Path | str, reference: Path | str, *, out_dir: Path | str | None = None,
                    strength: float = 0.7, match_dynamics: bool = False, fmt: str = "wav") -> MatchResult:
    from navig_audio.enhance.engine import EnhanceError, _run, export
    from navig_audio.mastering.loudness import measure
    from navig_audio.mastering.master import MasterError, to_loudness

    src, reference = Path(src), Path(reference)
    if not 0.0 < strength <= 1.0:
        raise MatchError("strength must be between 0 and 1")
    try:
        s_bal, r_bal = _balance(src), _balance(reference)
        s_rep, r_rep = analyze(src), analyze(reference)
    except AnalyzeError as exc:
        raise MatchError(str(exc)) from exc
    r_lufs = measure(reference).lufs
    if not math.isfinite(r_lufs):
        raise MatchError("the reference is silent")
    curve = eq_curve(s_bal, r_bal, strength)
    chain = []
    if curve:
        entries = ";".join(f"entry({f:.0f},{g:.2f})" for f, g in sorted(curve.items()))
        chain.append(f"firequalizer=gain_entry='{entries}'")
    dynamics = "kept"
    if match_dynamics and s_rep.crest_db - r_rep.crest_db >= 3.0:
        ratio = min(4.0, 1.5 + (s_rep.crest_db - r_rep.crest_db) / 4)
        chain.append(f"acompressor=threshold=-22dB:ratio={ratio:.1f}:attack=20:release=200:makeup=1")
        dynamics = f"compressed (ratio {ratio:.1f}) toward the reference's crest factor"
    dest = Path(out_dir) if out_dir else src.parent
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="navig-match-") as tmp:
        work = Path(tmp)
        toned = work / "toned.wav"
        try:
            _run(["-i", str(src), *(["-af", ",".join(chain)] if chain else []),
                  "-ar", str(s_rep.sample_rate), "-c:a", "pcm_f32le", str(toned)], toned, "the tone match", None)
            levelled = work / "levelled.wav"
            to_loudness(toned, levelled, lufs=r_lufs, tp=-1.0, margin_db=0.9 if fmt == "mp3" else 0.3)
            out = export(levelled, src, dest, [fmt], label="matched")[0]
        except (EnhanceError, MasterError) as exc:
            raise MatchError(str(exc)) from exc
    after = _balance(out)
    return MatchResult(
        output=str(out), reference=str(reference), strength=strength,
        eq_db={f"{f:.0f} Hz": g for f, g in sorted(curve.items())},
        tone_distance_before_db=tone_distance(s_bal, r_bal), tone_distance_after_db=tone_distance(after, r_bal),
        lufs_before=measure(src).lufs, lufs_after=measure(out).lufs, lufs_reference=r_lufs, dynamics=dynamics,
    )
