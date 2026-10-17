"""Loudness delivery: does a file meet a platform's loudness rules — and if not, fix it.

Targets are the published normalisation / delivery specs (integrated LUFS and a true
peak ceiling). Streaming services normalise playback, so "too loud" is not a failure
there, only wasted headroom; it is reported as such. Broadcast specs are strict.
Every number is measured (ffmpeg EBU R128); a fix is gain + a true-peak limiter,
re-measured until it lands, and never written over the original.
"""

from __future__ import annotations

import math
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from navig_audio.mastering.loudness import LoudnessError, measure

# name → (integrated LUFS, true-peak ceiling dBTP, tolerance LU, strict, what it is)
TARGETS: dict[str, tuple[float, float, float, bool, str]] = {
    "youtube": (-14.0, -1.0, 1.0, False, "YouTube playback normalisation"),
    "spotify": (-14.0, -1.0, 1.0, False, "Spotify default normalisation"),
    "apple-music": (-16.0, -1.0, 1.0, False, "Apple Music Sound Check"),
    "tidal": (-14.0, -1.0, 1.0, False, "TIDAL normalisation"),
    "podcast": (-16.0, -1.0, 1.0, False, "Apple Podcasts / common podcast spec (stereo)"),
    "podcast-mono": (-19.0, -1.0, 1.0, False, "podcast spec for mono speech"),
    "ebu-r128": (-23.0, -1.0, 0.5, True, "EU broadcast (EBU R128)"),
    "atsc-a85": (-24.0, -2.0, 2.0, True, "US broadcast (ATSC A/85)"),
}


class DeliveryError(RuntimeError):
    """Unknown target, unreadable file, or ffmpeg failure."""


@dataclass
class DeliveryCheck:
    path: str
    target: str
    target_lufs: float
    ceiling_db: float
    tolerance_lu: float
    lufs: float
    true_peak_db: float
    lra: float
    verdict: str  # pass · too-quiet · too-loud · peaks-over · silent
    advice: str

    @property
    def ok(self) -> bool:
        return self.verdict == "pass"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["ok"] = self.ok
        return {k: (None if isinstance(v, float) and not math.isfinite(v) else v) for k, v in d.items()}


def check(path: Path | str, target: str) -> DeliveryCheck:
    if target not in TARGETS:
        raise DeliveryError(f"unknown target {target!r} — one of {', '.join(TARGETS)}")
    lufs_t, tp_t, tol, strict, label = TARGETS[target]
    try:
        m = measure(path)
    except LoudnessError as exc:
        raise DeliveryError(str(exc)) from exc
    if not math.isfinite(m.lufs):
        verdict, advice = "silent", "The file is silent — nothing to deliver."
    elif m.true_peak > tp_t + 0.05:
        verdict = "peaks-over"
        advice = f"True peak {m.true_peak:+.1f} dBTP is over the {tp_t:+.1f} dBTP ceiling — limit the peaks."
    elif m.lufs < lufs_t - tol:
        verdict = "too-quiet"
        advice = (f"{m.lufs:.1f} LUFS is {lufs_t - m.lufs:.1f} LU under the {lufs_t:.0f} LUFS target ({label})"
                  + ("" if strict else " — it will play quieter than most content there"))
    elif m.lufs > lufs_t + tol:
        if strict:
            verdict, advice = "too-loud", f"{m.lufs:.1f} LUFS is over the {lufs_t:.0f} LUFS broadcast spec."
        else:
            verdict = "pass"
            advice = (f"{m.lufs:.1f} LUFS is louder than {lufs_t:.0f}; {label} will turn it down "
                      f"{m.lufs - lufs_t:.1f} dB on playback (no penalty, but no gain either).")
    else:
        verdict, advice = "pass", f"Within {tol:g} LU of {lufs_t:.0f} LUFS and under the {tp_t:+.1f} dBTP ceiling."
    return DeliveryCheck(str(path), target, lufs_t, tp_t, tol, m.lufs, m.true_peak, m.lra, verdict, advice)


def fix(path: Path | str, target: str, *, out_dir: Path | str | None = None, fmt: str = "wav") -> tuple[Path, DeliveryCheck]:
    """Level ``path`` to ``target`` (gain + true-peak limiter, measured). Returns (file, re-check)."""
    from navig_audio.enhance.engine import FORMATS, export
    from navig_audio.mastering.master import MasterError, to_loudness

    if target not in TARGETS:
        raise DeliveryError(f"unknown target {target!r}")
    if fmt not in FORMATS:
        raise DeliveryError(f"format must be one of {', '.join(FORMATS)}")
    src = Path(path)
    lufs_t, tp_t, *_ = TARGETS[target]
    dest = Path(out_dir) if out_dir else src.parent
    dest.mkdir(parents=True, exist_ok=True)
    if not shutil.which("ffmpeg"):
        raise DeliveryError("ffmpeg is not on PATH")
    # A sample-peak limiter undershoots TRUE peak (inter-sample peaks), and an encode adds
    # more. So the EXPORTED file is measured, and the ceiling lowered by what it overshot.
    margin = 0.9 if fmt == "mp3" else 0.5
    out: Path | None = None
    result: DeliveryCheck | None = None
    with tempfile.TemporaryDirectory(prefix="navig-delivery-") as tmp:
        levelled = Path(tmp) / "levelled.wav"
        for _ in range(4):
            try:
                to_loudness(src, levelled, lufs=lufs_t, tp=tp_t, margin_db=margin)
            except MasterError as exc:
                raise DeliveryError(str(exc)) from exc
            if out is not None:
                out.unlink(missing_ok=True)
            out = export(levelled, src, dest, [fmt], label=target)[0]
            result = check(out, target)
            if result.true_peak_db <= tp_t + 0.05:
                break
            margin += result.true_peak_db - tp_t + 0.2
    if out is None or result is None:  # unreachable: the loop runs at least once
        raise DeliveryError("nothing was written")
    return out, result
