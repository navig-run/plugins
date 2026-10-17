"""The bed's beat grid — so cuts land on the music, not next to it.

No librosa: decode the bed to mono, build an onset envelope (positive spectral
flux), find the tempo by autocorrelation in 70–150 BPM, then the phase that
best matches the onsets. Good enough for a steady synth bed, which is all a
channel uses. Cached per ``(file, offset, duration)`` — the cache lives with
the workspace (``<ws>/sound/grids``), so the channel scripts and these verbs
compute each grid exactly once.

Ported verbatim from the channel workspace's ``beatgrid.py``: the cut times of
every rendered episode derive from this, so the algorithm must not drift.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import numpy as np

__all__ = ["bpm", "grid"]

SR, HOP = 11025, 256


def grid(path: Path, offset: float, dur: float, cache_dir: Path) -> list[float]:
    """Beat times (seconds, relative to the bed's offset) covering ``dur``."""
    key = hashlib.sha1(f"{path}|{offset}|{dur:.1f}".encode()).hexdigest()[:12]
    cache_dir = Path(cache_dir)
    cf = cache_dir / f"{key}.json"
    if cf.exists():
        return json.loads(cf.read_text(encoding="utf-8"))
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(offset), "-t", str(dur + 2),
         "-i", str(path), "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
        capture_output=True).stdout
    x = np.frombuffer(raw, np.float32)
    if len(x) < SR * 4:
        return []
    n = (len(x) - 1024) // HOP
    frames = np.lib.stride_tricks.as_strided(
        x, (n, 1024), (x.strides[0] * HOP, x.strides[0]))
    mag = np.abs(np.fft.rfft(frames * np.hanning(1024), axis=1))
    flux = np.maximum(np.diff(np.log1p(mag), axis=0), 0).sum(axis=1)
    flux = (flux - flux.mean()) / (flux.std() + 1e-9)
    fps = SR / HOP
    ac = np.correlate(flux, flux, "full")[len(flux) - 1:]
    lags = np.arange(int(fps * 60 / 150), int(fps * 60 / 70))
    period = lags[np.argmax(ac[lags])]                  # frames per beat
    phases = [flux[p::period].sum() for p in range(period)]
    p0 = int(np.argmax(phases))
    beats = [(p0 + k * period) / fps for k in range(int(len(flux) / period) + 1)]
    beats = [round(b, 3) for b in beats if b <= dur + 1]
    cache_dir.mkdir(parents=True, exist_ok=True)
    cf.write_text(json.dumps(beats), encoding="utf-8")
    return beats


def bpm(beats: list[float]) -> float:
    return 60 / float(np.median(np.diff(beats))) if len(beats) > 2 else 0.0
