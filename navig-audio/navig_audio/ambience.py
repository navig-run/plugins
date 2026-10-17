"""Generated ambience — ours, license-free, exactly the length of the cut.

The world under the tracks: rain, static or city weather, seeded per kind so a
re-render gets the same bed, written as a mono 44.1 kHz wav wherever the
caller says. Ported from the channel workspace's ``kinetic.py`` — the owner's
audio direction ("glitches and invert and reverb for voice and background
ambience, not only music") made these three shapes; the numbers are tuned.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["KINDS", "amb_wav"]

KINDS = ("rain", "static", "city")


def amb_wav(kind: str, secs: float, out_dir: Path, force: bool = False) -> Path:
    """Write (or reuse) ``amb-<kind>-<seconds>.wav`` in ``out_dir``.

    The seed is the kind, so the file is deterministic across runs and
    machines — a re-render layers the identical weather.
    """
    import wave as wavmod

    import numpy as np

    if kind not in KINDS:
        raise ValueError(f"unknown ambience {kind!r} — one of {', '.join(KINDS)}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"amb-{kind}-{int(round(secs))}.wav"
    if p.exists() and not force:
        return p
    sr = 44100
    n = int(sr * secs)
    rng = np.random.default_rng(sum(ord(c) for c in kind))
    if kind == "rain":
        x = np.convolve(rng.standard_normal(n), np.ones(5) / 5, "same")
        x *= 0.7 + 0.3 * np.sin(np.linspace(0, np.pi * 2 * secs / 5.7, n))
        x *= 0.55
    elif kind == "static":
        x = rng.standard_normal(n) * (rng.random(n) < 0.10) * 5.0
        x = np.clip(np.convolve(x, np.ones(3) / 3, "same"), -1.2, 1.2) * 0.5
    else:                                   # city: brown rumble, low and slow
        x = np.cumsum(rng.standard_normal(n))
        x /= np.abs(x).max() + 1e-9
        x = np.convolve(x, np.ones(96) / 96, "same") * 0.8
    pcm = (np.clip(x, -1, 1) * 32767).astype(np.int16)
    with wavmod.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return p
