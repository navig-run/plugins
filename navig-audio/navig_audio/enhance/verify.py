"""Check that an enhanced file is still the same recording — and better, not just louder.

The checks are diagnostics, not proof of perceptual fidelity: a pass means nothing
measurable went wrong (no lost or invented events, no pitch/tempo change, no new
content in bands that were empty, no new clipping, peaks under the ceiling, the
defects that were targeted are reduced). The listening comparison is still the
operator's — which is why every result carries the gain that loudness-matches it.

Tolerances depend on the mode, because the modes promise different things: Preserve
Original allows almost no tonal or envelope change; Cinematic allows a lot and is
always labelled creative.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from navig_audio.enhance.analysis import AudioReport, analyze, decode, frame_rms_db, onsets, spectrum_db

# mode → tolerance. env_corr/spec_corr: minimum Pearson r; centroid: max relative drift;
# onset_*: event matching; new_band_db: rise allowed in a band the source had (nearly)
# nothing in; dur_ms: unexplained duration change; pitch_bins: log-spectrum shift (1/48 oct).
TOLERANCE: dict[str, dict[str, float]] = {
    "preserve": {"env_corr": 0.97, "spec_corr": 0.985, "centroid": 0.08, "onset_recall": 0.9,
                 "onset_precision": 0.9, "onset_ms": 12, "new_band_db": 12, "dur_ms": 12, "pitch_bins": 1},
    "studio": {"env_corr": 0.93, "spec_corr": 0.95, "centroid": 0.18, "onset_recall": 0.85,
               "onset_precision": 0.85, "onset_ms": 15, "new_band_db": 15, "dur_ms": 12, "pitch_bins": 1},
    "custom": {"env_corr": 0.90, "spec_corr": 0.93, "centroid": 0.25, "onset_recall": 0.8,
               "onset_precision": 0.8, "onset_ms": 20, "new_band_db": 18, "dur_ms": 400, "pitch_bins": 1},
    "cinematic": {"env_corr": 0.80, "spec_corr": 0.85, "centroid": 0.35, "onset_recall": 0.75,
                  "onset_precision": 0.7, "onset_ms": 25, "new_band_db": 24, "dur_ms": 400, "pitch_bins": 2},
}
NOISY_SNR_DB = 25.0  # below this the source's event list is too unreliable to reject on
ONSET_LOOSE_DB = -36.0  # the analysis lists events at -30 dB; matching looks 6 dB deeper
# Which finding each stage is meant to reduce.
STAGE_TARGETS = {"declip": "clipping", "dc": "dc_offset", "denoise": "noise", "dehum": "hum",
                 "declick": "clicks", "limit": "too_hot", "gain": "very_quiet", "trim": "edge_silence"}


@dataclass
class Check:
    name: str
    ok: bool
    hard: bool  # a failed hard check rejects the candidate
    value: Any
    limit: Any
    detail: str = ""


@dataclass
class Verification:
    verdict: str  # pass · warn · fail
    checks: list[Check]
    match_gain_db: float  # apply to the OUTPUT to loudness-match it to the source for A/B
    creative: bool
    summary: str
    after: dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and c.hard]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["failed"] = [c.name for c in self.failed]
        return _clean(d)


def _clean(v: Any) -> Any:
    if isinstance(v, (float, np.floating)):
        f = float(v)
        return None if not math.isfinite(f) else round(f, 4)
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    if isinstance(v, (int, np.integer)):
        return int(v)
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    return v


def _pearson(a: np.ndarray, b: np.ndarray) -> float:
    if a.size < 3 or b.size < 3:
        return 1.0
    a, b = a - a.mean(), b - b.mean()
    den = math.sqrt(float((a**2).sum() * (b**2).sum()))
    return float((a * b).sum() / den) if den > 0 else 1.0


def _envelope(x: np.ndarray, sr: int, floor_db: float | None = None) -> np.ndarray:
    """10 ms loudness contour, clipped at 60 dB under its peak — or at ``floor_db``.

    ``floor_db`` is the SOURCE's own noise floor (+6 dB) when it has one: below it the
    contour is the noise bed, which is exactly what a denoiser is meant to change, so
    both sides are clipped there and only the sound above the bed is compared.
    """
    mono = x.mean(axis=0)
    env = frame_rms_db(mono - mono.mean(), sr, frame_s=0.01)  # DC is not content
    top = np.max(env) if env.size else 0.0
    clip = top - 60.0 if floor_db is None else max(top - 60.0, floor_db)
    return np.maximum(env, clip)


def _lag_frames(a: np.ndarray, b: np.ndarray, max_lag: int) -> int:
    """Best lag (frames) of b against a, from envelope cross-correlation."""
    la, lb = 10 ** (a / 20), 10 ** (b / 20)
    best, best_lag = -1e18, 0
    for lag in range(-max_lag, max_lag + 1):
        if lag >= 0:
            x, y = la[lag:], lb[: la.size - lag]
        else:
            x, y = la[: la.size + lag], lb[-lag:]
        n = min(x.size, y.size)
        if n < 3:
            continue
        s = float(np.dot(x[:n], y[:n]))
        if s > best:
            best, best_lag = s, lag
    return best_lag


def _third_octave(sr: int, mono: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    freqs, pdb = spectrum_db(mono, sr)
    p = 10 ** (pdb / 10)
    centres = 1000 * 2 ** (np.arange(-17, 15) / 3)  # ~20 Hz … 20 kHz
    centres = centres[centres < sr / 2 * 0.95]
    levels = []
    for c in centres:
        lo, hi = c / 2 ** (1 / 6), c * 2 ** (1 / 6)
        m = (freqs >= lo) & (freqs < hi)
        levels.append(10 * math.log10(float(p[m].sum()) + 1e-20) if m.any() else -200.0)
    return centres, np.array(levels)


def _content_samples(a: np.ndarray, b: np.ndarray, sr: int, *, ea_full: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The samples of both signals where the SOURCE is within 20 dB of its loudest."""
    hop = int(sr * 0.01)
    loud = np.nonzero(ea_full >= ea_full.max() - 20)[0]
    if loud.size < 5:
        return a.mean(axis=0), b.mean(axis=0)
    ma, mb = a.mean(axis=0), b.mean(axis=0)
    n = min(ma.size, mb.size)
    idx = np.concatenate([np.arange(i * hop, min((i + 1) * hop, n)) for i in loud if i * hop < n])
    return ma[idx], mb[idx]


def _noise_spectrum(x: np.ndarray, sr: int) -> np.ndarray | None:
    """Third-octave levels of the quietest fifth of the 20 ms frames — the noise bed."""
    mono = x.mean(axis=0)
    mono = mono - mono.mean()
    n = int(sr * 0.02)
    rms = frame_rms_db(mono, sr)
    live = np.nonzero(rms > -100)[0]
    if live.size < 20:
        return None
    quiet = live[rms[live] <= np.percentile(rms[live], 20)]
    seg = np.concatenate([mono[i * n:(i + 1) * n] for i in quiet])
    if seg.size < 4096:
        return None
    return _third_octave(sr, seg)[1]


def _pitch_shift_bins(sr: int, a: np.ndarray, b: np.ndarray) -> int:
    """Shift (1/48-octave bins) that best aligns b's log-frequency spectrum with a's."""
    fa, pa = spectrum_db(a, sr)
    _, pb = spectrum_db(b, sr)
    hi = min(sr / 2 * 0.9, 12_000.0)
    grid = 100 * 2 ** (np.arange(0, int(48 * math.log2(hi / 100))) / 48)
    ga = np.interp(grid, fa, pa)
    gb = np.interp(grid, fa, pb)
    ga, gb = ga - ga.mean(), gb - gb.mean()
    best, best_s = -1e18, 0
    for s in range(-24, 25):
        x = ga[max(0, s): ga.size + min(0, s)]
        y = gb[max(0, -s): gb.size + min(0, -s)]
        r = _pearson(x, y)
        # Prefer no shift on ties — a noisy spectrum must not invent one.
        if r > best + 1e-4:
            best, best_s = r, s
    return best_s


def _match(src: list[float], out: list[float], tol_s: float) -> tuple[float, float, list[float], list[float]]:
    used: set[int] = set()
    hits = 0
    for t in src:
        j = next((i for i, u in enumerate(out) if i not in used and abs(u - t) <= tol_s), None)
        if j is not None:
            used.add(j)
            hits += 1
    missing = [t for t in src if not any(abs(u - t) <= tol_s for u in out)]
    extra = [u for i, u in enumerate(out) if i not in used]
    recall = hits / len(src) if src else 1.0
    precision = hits / len(out) if out else (1.0 if not src else 0.0)
    return recall, precision, missing, extra


def verify(source: AudioReport, output: Path, *, mode: str, stages: list[str], true_peak_db: float,
           trim_lead_s: float = 0.0, creative: bool = False) -> Verification:
    tol = TOLERANCE.get(mode, TOLERANCE["studio"])
    checks: list[Check] = []
    out_rep = analyze(output, category=source.category)
    sr = source.sample_rate

    def add(name: str, ok: bool, value: Any, limit: Any, detail: str = "", hard: bool = True) -> None:
        checks.append(Check(name, bool(ok), hard, value, limit, detail))

    add("format", out_rep.sample_rate == sr and out_rep.channels == source.channels,
        f"{out_rep.sample_rate} Hz · {out_rep.channels} ch", f"{sr} Hz · {source.channels} ch",
        "sample rate and channel count are kept")
    if not math.isfinite(out_rep.rms_db) or out_rep.rms_db < -120:
        add("signal", False, "silent", "audible", "the output is silent")
        return Verification("fail", checks, 0.0, creative, "The output is silent — rejected.", out_rep.to_dict())

    a = decode(Path(source.path), sr, source.channels)
    b = decode(output, sr, source.channels)
    expected = source.decoded_duration_s - (trim_lead_s if "trim" in stages else 0.0)
    if "trim" in stages:
        expected -= max(0.0, source.trail_silence_s - 0.1)
    dur_err_ms = (out_rep.decoded_duration_s - expected) * 1000
    add("duration", abs(dur_err_ms) <= tol["dur_ms"] + (60 if "trim" in stages else 0),
        f"{out_rep.decoded_duration_s:.3f}s", f"{expected:.3f}s ± {tol['dur_ms']:.0f} ms",
        "no truncation, no stretched time")

    bed = (source.noise_floor_db + 6.0) if source.noise_floor_db is not None and source.finding("noise") else None
    ea, eb = _envelope(a, sr, bed), _envelope(b, sr, bed)
    lead_frames = int(round((trim_lead_s if "trim" in stages else 0.0) / 0.01))
    if lead_frames:
        ea = ea[lead_frames:]
    lag = _lag_frames(ea, eb, max_lag=10)
    add("alignment", abs(lag) <= 1, f"{lag * 10} ms", "≤ 10 ms", "events stay where they were")
    n = min(ea.size, eb.size)
    env_r = _pearson(ea[:n], eb[:n])
    add("envelope", env_r >= tol["env_corr"], round(env_r, 4), f"≥ {tol['env_corr']}",
        "the loudness contour over time is the same sound")

    # Hysteresis: a clear event on one side must exist (even if weaker) on the other —
    # strict detections are matched against loose ones, so an event sitting right at the
    # detection threshold can't flip between "kept" and "lost" on a 0.5 dB change.
    offset = trim_lead_s if "trim" in stages else 0.0
    src_strict = [t - offset for t in source.onsets_s if t - offset >= 0]
    src_loose = [t - offset for t in onsets(a.mean(axis=0), sr, rel_db=ONSET_LOOSE_DB) if t - offset >= 0]
    out_strict = onsets(b.mean(axis=0), sr)
    out_loose = onsets(b.mean(axis=0), sr, rel_db=ONSET_LOOSE_DB)
    tol_s = tol["onset_ms"] / 1000
    rec, _, missing, _ = _match(src_strict, out_loose, tol_s)
    _, _, extra, _ = _match(out_strict, src_loose, tol_s)
    # An output event the source's onset detector missed is only INVENTED if the source
    # had nothing there: a syllable masked by hiss is still in the source above its
    # floor, and a denoiser that reveals it has not made it up (measured: speech at
    # ~20 dB SNR showed 4 detectable onsets before denoising and 48 after).
    src_env = frame_rms_db(a.mean(axis=0) - a.mean(), sr, frame_s=0.01)
    floor = source.noise_floor_db if source.noise_floor_db is not None else -90.0

    def _present_in_source(t: float) -> bool:
        # Present = the source RISES there too (an attack above the bed), not merely has
        # energy: a new event dropped onto an older sound's decaying tail is invented.
        i = int((t + offset) / 0.01)
        win = src_env[max(0, i - 3): i + 4]
        before = src_env[max(0, i - 15): max(0, i - 5)]
        if not win.size or not before.size:
            return False
        peak = float(win.max())
        return peak > floor + 6.0 and peak - float(np.median(before)) >= 3.0

    extra = [t for t in extra if not _present_in_source(t)]
    prec = 1.0 - (len(extra) / len(out_strict)) if out_strict else 1.0
    # Under a heavy noise bed the source's own event list is unreliable (measured: speech
    # at ~18 dB SNR yields 3 detectable onsets, and a 10 Hz high-pass alone flips one), so
    # there the event checks inform instead of reject; alignment, envelope above the bed,
    # tone, pitch and new-content stay hard.
    masked = source.snr_db is not None and source.snr_db < NOISY_SNR_DB
    note = " (source too noisy to count events reliably)" if masked else ""
    add("events_kept", rec >= tol["onset_recall"], f"{rec:.0%}", f"≥ {tol['onset_recall']:.0%}",
        (f"missing at {', '.join(f'{t:.3f}s' for t in missing[:5])}" if missing else "every event is still there") + note,
        hard=not masked)
    add("no_new_events", prec >= tol["onset_precision"], f"{prec:.0%}", f"≥ {tol['onset_precision']:.0%}",
        (f"new at {', '.join(f'{t:.3f}s' for t in extra[:5])}" if extra else "no invented events") + note,
        hard=not creative and not masked)

    _, la = _third_octave(sr, a.mean(axis=0))
    _, lb = _third_octave(sr, b.mean(axis=0))
    m = min(la.size, lb.size)
    la, lb = la[:m], lb[:m]
    ref = la.max()
    # Tone is judged on the CONTENT: the frames where the source is loud, in the bands
    # that carry it (within 30 dB of its strongest band). Removing a noise bed lowers
    # noise-only bands by design; that must not read as a change of tone.
    shift = int(round((trim_lead_s if "trim" in stages else 0.0) * sr))
    a_al = a[:, shift:]  # align the source with a trimmed output before frame-by-frame use
    ca, cb = _content_samples(a_al, b, sr, ea_full=_envelope(a_al, sr))
    _, ta = _third_octave(sr, ca)
    _, tb = _third_octave(sr, cb)
    k = min(ta.size, tb.size)
    ta, tb = ta[:k], tb[:k]
    live = ta > ta.max() - 30
    denoised = source.finding("noise") is not None and "denoise" in stages
    if denoised:
        # Compare only the bands where the sound stands >= 10 dB above the source's own
        # noise spectrum (from its quietest frames). Under a hiss bed the high bands are
        # mostly hiss even while someone speaks; removing it is the point, not a change
        # of tone (measured: r 0.86 on a clean denoise when those bands were counted).
        nz = _noise_spectrum(a_al, sr)
        if nz is not None and nz.size >= ta.size:
            live &= ta - nz[: ta.size] >= 10.0
    spec_r = _pearson(ta[live] - ta[live].mean(), tb[live] - tb[live].mean()) if live.sum() >= 3 else 1.0
    add("tone", spec_r >= tol["spec_corr"], round(spec_r, 4), f"≥ {tol['spec_corr']}",
        "the spectrum of the sound itself keeps its shape")
    empty = la < ref - 60
    shift = (lb - lb.max()) - (la - ref)
    rises = shift[empty]
    worst = float(rises.max()) if rises.size else 0.0
    add("no_new_content", worst <= tol["new_band_db"], f"{worst:+.1f} dB", f"≤ +{tol['new_band_db']:.0f} dB",
        "no energy appears in bands that were empty (a sign of generated content)", hard=not creative)
    cen_drift = abs(out_rep.spectral_centroid_hz - source.spectral_centroid_hz) / max(1.0, source.spectral_centroid_hz)
    add("brightness", cen_drift <= tol["centroid"], f"{cen_drift:.1%}", f"≤ {tol['centroid']:.0%}",
        "overall brightness stays in range" + (" (hiss removed — brightness drops by design)" if denoised else ""),
        hard=not denoised)
    pshift = _pitch_shift_bins(sr, a.mean(axis=0), b.mean(axis=0))
    add("pitch", abs(pshift) <= tol["pitch_bins"], f"{pshift * 25:+d} cents", f"≤ {int(tol['pitch_bins'] * 25)} cents",
        "no pitch change")

    add("no_new_clipping", out_rep.clip_events <= (0 if "declip" in stages else source.clip_events),
        out_rep.clip_events, 0 if "declip" in stages else source.clip_events, "no new distortion")
    if math.isfinite(out_rep.true_peak_db):
        ceiling = max(true_peak_db, source.true_peak_db) if not any(s in stages for s in ("limit", "loudness", "gain")) \
            else true_peak_db
        add("true_peak", out_rep.true_peak_db <= ceiling + 0.3, f"{out_rep.true_peak_db:+.1f} dBTP",
            f"≤ {ceiling:+.1f} dBTP", "peaks stay under the ceiling")
    for stage in stages:
        code = STAGE_TARGETS.get(stage)
        if not code or source.finding(code) is None:
            continue
        still = out_rep.finding(code)
        better = still is None or (still.severity != "major" and source.finding(code).severity == "major")
        add(f"fixed_{code}", better, "resolved" if still is None else f"still {still.severity}",
            "reduced", source.finding(code).title, hard=False)
    before = {f.code for f in source.findings if f.severity != "info"}
    new_major = [f.title for f in out_rep.findings if f.severity == "major" and f.code not in before]
    add("no_new_defects", not new_major, ", ".join(new_major) or "none", "none", "processing added no defect")

    if math.isfinite(source.lufs) and math.isfinite(out_rep.lufs) and not source.short_clip:
        gain = source.lufs - out_rep.lufs
    else:
        gain = source.rms_db - out_rep.rms_db
    hard_fail = [c for c in checks if c.hard and not c.ok]
    soft_fail = [c for c in checks if not c.hard and not c.ok]
    if hard_fail:
        verdict = "fail"
        summary = "Rejected: " + "; ".join(f"{c.name} ({c.value} vs {c.limit})" for c in hard_fail)
    elif creative or soft_fail:
        verdict = "warn"
        summary = "Creative treatment — compare by ear before keeping it." if creative else \
            "Kept the content intact, but: " + "; ".join(c.detail or c.name for c in soft_fail)
    else:
        verdict = "pass"
        summary = "Same recording: every event, its timing, pitch and tone are intact."
    return Verification(verdict, checks, round(gain, 2), creative, summary, out_rep.to_dict())
