"""Measure an audio file and name its real defects — deterministic DSP only.

Every number here comes from ffprobe/ffmpeg or a numpy computation on the decoded
samples. Nothing is estimated by a model. The content *category* (speech, sfx, music,
ambience) is a rules-based guess from the measurements, labelled with its confidence,
and the user can override it.

What counts as a defect is deliberately narrow:

* **noise** only when the quiet gaps *between* events hold broadband, stationary
  energy (high spectral flatness). Tonal ringing and decays — a cash register's
  metallic tail — are spectrally peaky and are content, not noise.
* **clicks** only as isolated, very short spikes far above their surroundings, and
  never auto-fixed for sound effects, where sharp transients are the point.
* **level** only when it is far outside a usable range (clipping, true peak over
  0 dBTP, or very quiet). A clean file at a modest level is not a defect.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from navig_sdk.host import decode_console_result

# ── limits ───────────────────────────────────────────────────────────
MAX_FILE_BYTES = 1_000_000_000
MAX_DURATION_S = 1800.0  # analysis decodes into memory; longer files are refused, not truncated
LOSSY_CODECS = {"mp3", "mp2", "aac", "vorbis", "opus", "wmav2", "wmav1", "ac3", "eac3", "amr_nb", "amr_wb"}

# ── thresholds (dB unless named otherwise) ───────────────────────────
CLIP_LEVEL = 0.999            # |x| at or above this is a clipped sample
CLIP_RUN = 3                  # consecutive clipped samples that make one clip event
DC_MINOR = 0.005              # mean offset (linear) worth removing
DC_MAJOR = 0.02
SILENCE_DB = -60.0            # edge silence threshold (frame RMS)
EDGE_SILENCE_MIN_S = 0.35     # leading/trailing silence longer than this is reported
TOO_HOT_TP = -0.1             # true peak above this (dBTP) risks inter-sample clipping
QUIET_LUFS = -32.0            # integrated loudness below this is "very quiet"
NOISE_FLATNESS = 0.30         # spectral flatness of gap frames above this = broadband noise
NOISE_SNR_MINOR = 45.0        # event-to-gap level difference below this: audible noise floor
NOISE_SNR_MAJOR = 30.0
HUM_PROMINENCE = 18.0         # mains peak over its neighbourhood
CLICK_RATIO = 20.0            # spike over local RMS
CLICKS_MINOR = 3
NARROW_BAND_HZ = 11_000.0     # content cut off below this (with a higher Nyquist) = band-limited
SHORT_CLIP_S = 3.0            # gated LUFS is unreliable below this


class AnalyzeError(RuntimeError):
    """The file can't be analysed (missing ffmpeg, unreadable, too large, not audio)."""


@dataclass
class Finding:
    code: str
    severity: str  # info · minor · major
    title: str  # plain language, for the default UI
    evidence: str  # the measured numbers behind it
    fix_stage: str | None  # the processing stage that addresses it, if any
    confidence: str = "high"  # high · medium · low


@dataclass
class AudioReport:
    path: str
    container: str
    codec: str
    lossy: bool
    bit_rate: int | None
    sample_rate: int
    channels: int
    duration_s: float  # container (ffprobe) duration
    decoded_duration_s: float  # samples / rate — what is actually there
    lufs: float
    lra: float
    true_peak_db: float
    peak_db: float
    rms_db: float
    crest_db: float
    dc_offset: list[float]
    clip_events: int
    clipped_samples: int
    noise_floor_db: float | None
    gap_flatness: float | None
    snr_db: float | None
    lead_silence_s: float
    trail_silence_s: float
    bandwidth_hz: float
    hum_hz: int | None
    hum_prominence_db: float | None
    clicks: int
    onsets_s: list[float]
    stereo_correlation: float | None
    side_ratio_db: float | None
    band_shares: dict[str, float]
    spectral_centroid_hz: float
    category: str
    category_confidence: str
    category_reason: str
    short_clip: bool
    findings: list[Finding] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return _finite(d)

    def finding(self, code: str) -> Finding | None:
        return next((f for f in self.findings if f.code == code), None)


def _finite(v: Any) -> Any:
    """JSON-safe: -inf/nan → None (reported as 'no signal'), rounded floats."""
    if isinstance(v, float):
        return None if not math.isfinite(v) else round(v, 4)
    if isinstance(v, dict):
        return {k: _finite(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_finite(x) for x in v]
    return v


# ── ffmpeg plumbing ──────────────────────────────────────────────────


def _exe(name: str) -> str:
    exe = shutil.which(name)
    if not exe:
        raise AnalyzeError(f"{name} is not on PATH — install ffmpeg (scoop install ffmpeg) and try again")
    return exe


def probe(path: Path) -> dict[str, Any]:
    proc = decode_console_result(subprocess.run(
        [_exe("ffprobe"), "-v", "error", "-show_entries",
         "format=format_name,duration,bit_rate:stream=codec_type,codec_name,sample_rate,channels,bit_rate",
         "-of", "json", str(path)],
        capture_output=True, timeout=60, check=False,
    ))
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()
        raise AnalyzeError(f"{path.name} is not a readable media file ({tail[-1] if tail else 'ffprobe failed'})")
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError as exc:
        raise AnalyzeError(f"ffprobe returned unreadable output for {path.name}") from exc
    audio = [s for s in data.get("streams") or [] if s.get("codec_type") == "audio"]
    if not audio:
        raise AnalyzeError(f"{path.name} has no audio stream")
    s, fmt = audio[0], data.get("format") or {}

    def _int(x: Any) -> int | None:
        try:
            return int(x)
        except (TypeError, ValueError):
            return None

    return {
        "container": str(fmt.get("format_name") or ""),
        "codec": str(s.get("codec_name") or ""),
        "sample_rate": _int(s.get("sample_rate")) or 0,
        "channels": _int(s.get("channels")) or 0,
        "duration_s": float(fmt.get("duration") or 0.0),
        "bit_rate": _int(s.get("bit_rate")) or _int(fmt.get("bit_rate")),
    }


def decode(path: Path, sample_rate: int, channels: int, *, timeout: int = 600) -> np.ndarray:
    """Decode to float32, shape (channels, samples), at the file's own rate and layout."""
    proc = subprocess.run(
        [_exe("ffmpeg"), "-hide_banner", "-nostdin", "-v", "error", "-i", str(path), "-vn",
         "-f", "f32le", "-acodec", "pcm_f32le", "-ar", str(sample_rate), "-ac", str(channels), "-"],
        capture_output=True, timeout=timeout, check=False,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise AnalyzeError(f"could not decode {path.name}: {err[-1] if err else 'ffmpeg failed'}")
    x = np.frombuffer(proc.stdout, dtype="<f4")
    if x.size == 0:
        raise AnalyzeError(f"{path.name} decoded to no samples")
    x = x[: x.size - (x.size % channels)]
    return x.reshape(-1, channels).T.copy()


def loudness(path: Path) -> tuple[float, float, float]:
    """(integrated LUFS, LRA, true peak dBTP) via ffmpeg's EBU R128 meter."""
    from navig_audio.mastering.loudness import LoudnessError, measure

    try:
        m = measure(path)
    except LoudnessError as exc:
        raise AnalyzeError(str(exc)) from exc
    return m.lufs, m.lra, m.true_peak


# ── DSP helpers ──────────────────────────────────────────────────────


def _db(x: float, floor: float = -200.0) -> float:
    return 20 * math.log10(x) if x > 0 else floor


def frame_rms_db(mono: np.ndarray, sr: int, frame_s: float = 0.02) -> np.ndarray:
    n = max(1, int(sr * frame_s))
    k = mono.size // n
    if k == 0:
        return np.array([_db(float(np.sqrt(np.mean(mono**2))) if mono.size else 0.0)])
    frames = mono[: k * n].reshape(k, n)
    rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))
    with np.errstate(divide="ignore"):
        return np.where(rms > 0, 20 * np.log10(rms), -200.0)


def _stft_mag(mono: np.ndarray, sr: int, n_fft: int = 2048, hop: int = 512) -> np.ndarray:
    if mono.size < n_fft:
        mono = np.pad(mono, (0, n_fft - mono.size))
    win = np.hanning(n_fft).astype(np.float32)
    count = 1 + (mono.size - n_fft) // hop
    idx = np.arange(n_fft)[None, :] + hop * np.arange(count)[:, None]
    return np.abs(np.fft.rfft(mono[idx] * win, axis=1)).astype(np.float32)


def onsets(mono: np.ndarray, sr: int, *, hop: int = 512, rel_db: float = -30.0) -> list[float]:
    """Onset times (s) from positive spectral flux with adaptive peak picking.

    Only onsets whose flux is within ``rel_db`` of the strongest are kept, so the list
    names the audible events (impacts, syllables, notes), not texture. The signal is
    peak-normalised first: the same recording at a different gain must yield the
    same events (the log compression below is level-dependent otherwise).
    """
    peak = float(np.abs(mono).max()) if mono.size else 0.0
    if peak > 0:
        mono = mono / peak
    n_fft = 2048
    mag = _stft_mag(mono, sr, n_fft=n_fft, hop=hop)
    if mag.shape[0] < 3:
        return []
    logm = np.log1p(mag * 100.0)
    # Adaptive whitening: subtract each bin's steady level (its 20th percentile over
    # time). A stationary hiss bed otherwise lifts every frame's flux and hides the
    # attacks under it — measured: 0 onsets found in speech at ~20 dB SNR.
    if logm.shape[0] >= 10:
        logm = np.maximum(0.0, logm - np.percentile(logm, 20, axis=0, keepdims=True))
    flux = np.maximum(0.0, np.diff(logm, axis=0)).sum(axis=1)
    if not np.any(flux > 0):
        return []
    peak = float(flux.max())
    floor = peak * (10 ** (rel_db / 20))
    w = max(3, int(0.05 * sr / hop))  # 50 ms neighbourhood
    out: list[float] = []
    last = -1e9
    for i in range(1, flux.size - 1):
        lo, hi = max(0, i - w), min(flux.size, i + w + 1)
        local = flux[lo:hi]
        if flux[i] < floor or flux[i] < local.max() or flux[i] <= np.median(local) * 1.5:
            continue
        t = ((i + 1) * hop + n_fft / 2) / sr  # the frame's centre, not its start
        if t - last >= 0.03:
            out.append(round(t, 4))
            last = t
    return out


def spectrum_db(mono: np.ndarray, sr: int, n_fft: int = 8192) -> tuple[np.ndarray, np.ndarray]:
    """Long-term average power spectrum (dB) and its frequency axis."""
    mag = _stft_mag(mono, sr, n_fft=n_fft, hop=n_fft // 2)
    power = np.mean(mag.astype(np.float64) ** 2, axis=0)
    freqs = np.fft.rfftfreq(n_fft, 1 / sr)
    with np.errstate(divide="ignore"):
        return freqs, np.where(power > 0, 10 * np.log10(power), -200.0)


BANDS = {"sub": (0, 60), "low": (60, 250), "low_mid": (250, 2000), "high_mid": (2000, 6000), "air": (6000, 1e9)}


def band_shares(freqs: np.ndarray, power_db: np.ndarray) -> dict[str, float]:
    p = 10 ** (power_db / 10)
    total = float(p.sum()) or 1.0
    return {k: round(float(p[(freqs >= lo) & (freqs < hi)].sum()) / total, 4) for k, (lo, hi) in BANDS.items()}


def _bandwidth(freqs: np.ndarray, power_db: np.ndarray) -> float:
    """Highest frequency still within 60 dB of the spectrum's peak (smoothed)."""
    if power_db.size < 8:
        return 0.0
    k = 9
    smooth = np.convolve(power_db, np.ones(k) / k, mode="same")
    peak = float(smooth[freqs > 40].max()) if np.any(freqs > 40) else float(smooth.max())
    live = np.nonzero(smooth >= peak - 60.0)[0]
    return float(freqs[live[-1]]) if live.size else 0.0


def _hum(mono: np.ndarray, sr: int) -> tuple[int | None, float | None]:
    """Mains hum: the 50/60 Hz fundamental's prominence over its neighbourhood.

    Uses its own long FFT (~1 Hz bins). At the ~5 Hz resolution of the general
    spectrum a 50 Hz tone leaks into the 60 Hz bin and the two can't be told apart.
    """
    n_fft = 1 << int(min(16, max(12, math.floor(math.log2(max(mono.size, 4096))))))
    freqs, power_db = spectrum_db(mono, sr, n_fft=n_fft)
    best: tuple[int | None, float | None] = (None, None)
    for f0 in (50, 60):
        at = np.abs(freqs - f0) <= 1.0
        near = (np.abs(freqs - f0) > 3) & (np.abs(freqs - f0) <= 8)
        if not at.any() or not near.any():
            continue
        prom = float(power_db[at].max() - np.median(power_db[near]))
        if best[1] is None or prom > best[1]:
            best = (f0, round(prom, 2))
    return best


def _clip_events(x: np.ndarray) -> tuple[int, int]:
    events = samples = 0
    for ch in x:
        hot = np.abs(ch) >= CLIP_LEVEL
        samples += int(hot.sum())
        if not hot.any():
            continue
        d = np.diff(np.concatenate(([0], hot.astype(np.int8), [0])))
        starts, ends = np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]
        events += int(np.sum((ends - starts) >= CLIP_RUN))
    return events, samples


def _clicks(mono: np.ndarray, sr: int, event_times: list[float] | None = None) -> int:
    """Isolated spikes ≤ 1 ms that stand CLICK_RATIO dB above the surrounding 20 ms.

    A spike at the start of a detected event is that event's attack (content), not a
    click, so spikes within 10 ms of an onset are not counted.
    """
    if mono.size < sr // 10:
        return 0
    d2 = np.abs(np.diff(mono, n=2))
    win = max(8, int(sr * 0.02))
    kernel = np.ones(win) / win
    local = np.sqrt(np.convolve(d2.astype(np.float64) ** 2, kernel, mode="same")) + 1e-9
    ratio = d2 / local
    spikes = np.nonzero(ratio > 10 ** (CLICK_RATIO / 20))[0]
    if spikes.size == 0:
        return 0
    groups = np.split(spikes, np.nonzero(np.diff(spikes) > int(sr * 0.001))[0] + 1)
    near = np.array(event_times or [], dtype=np.float64)
    count = 0
    for g in groups:
        if not g.size or (g[-1] - g[0]) > int(sr * 0.001):
            continue
        t = g[0] / sr
        if near.size and float(np.min(np.abs(near - t))) <= 0.025:
            continue
        count += 1
    return count


def _gaps(rms_db: np.ndarray) -> np.ndarray:
    """The quietest fifth of the non-silent frames — where a noise floor shows.

    Relative to the material's own distribution, not a fixed distance under its loud
    part: a recording at ~20 dB SNR has NO frame 25 dB under its peaks, which made
    heavy noise read as "no noise". Whether those frames ARE noise is decided by their
    spectral flatness (broadband) and the event-to-floor distance, not by this mask.
    """
    live = rms_db[rms_db > -100]
    if live.size < 10:
        return np.zeros_like(rms_db, dtype=bool)
    cut = np.percentile(live, 20)
    return (rms_db > -100) & (rms_db <= cut)


def _gap_flatness(mono: np.ndarray, sr: int, gap_mask: np.ndarray, frame_s: float = 0.02) -> float | None:
    n = int(sr * frame_s)
    idx = np.nonzero(gap_mask)[0]
    if idx.size < 10:
        return None
    vals = []
    for i in idx[:: max(1, idx.size // 200)]:
        seg = mono[i * n:(i + 1) * n]
        if seg.size < n:
            continue
        p = np.abs(np.fft.rfft(seg * np.hanning(seg.size))) ** 2 + 1e-20
        p = p[1:]
        vals.append(float(np.exp(np.mean(np.log(p))) / np.mean(p)))
    return round(float(np.median(vals)), 4) if vals else None


def _edge_silence(rms_db: np.ndarray, frame_s: float = 0.02) -> tuple[float, float]:
    loud = np.nonzero(rms_db > SILENCE_DB)[0]
    if loud.size == 0:
        return len(rms_db) * frame_s, 0.0
    return round(loud[0] * frame_s, 3), round((len(rms_db) - 1 - loud[-1]) * frame_s, 3)


def classify(duration: float, onset_count: int, shares: dict[str, float], gaps_frac: float,
             crest_db: float) -> tuple[str, str, str]:
    """(category, confidence, reason) from measurements only."""
    speechy = shares.get("low_mid", 0) + shares.get("high_mid", 0)
    if duration <= 4.0 and onset_count <= 12:
        return "sfx", "high" if duration <= 2.5 else "medium", f"short ({duration:.2f}s) with {onset_count} event(s)"
    rate = onset_count / max(duration, 1e-6)
    if speechy > 0.6 and 1.5 <= rate <= 8 and gaps_frac > 0.08:
        return "speech", "medium", f"energy mostly 250 Hz–6 kHz, {rate:.1f} events/s, pauses"
    if duration > 20 and rate < 0.4 and crest_db < 14:
        return "ambience", "medium", "long, steady, few distinct events"
    if duration > 8:
        return "music", "low", "long material with recurring events"
    return "mixed", "low", "no clear profile"


# ── the analysis ─────────────────────────────────────────────────────


def analyze(path: Path | str, *, category: str = "auto") -> AudioReport:
    path = Path(path)
    if not path.is_file():
        raise AnalyzeError(f"no such file: {path}")
    size = path.stat().st_size
    if size == 0:
        raise AnalyzeError(f"{path.name} is empty")
    if size > MAX_FILE_BYTES:
        raise AnalyzeError(f"{path.name} is larger than {MAX_FILE_BYTES // 1_000_000} MB")
    info = probe(path)
    if info["duration_s"] > MAX_DURATION_S:
        raise AnalyzeError(f"{path.name} is longer than {int(MAX_DURATION_S // 60)} minutes")
    if not info["sample_rate"] or not info["channels"]:
        raise AnalyzeError(f"{path.name}: unknown sample rate or channel count")
    sr, nch = info["sample_rate"], info["channels"]
    x = decode(path, sr, nch)
    # Frame-level measurements run on the DC-free signal: an offset otherwise reads as
    # signal in every gap and hides the noise floor (and inflates RMS). The offset
    # itself is still measured on the raw samples below.
    mono = x.mean(axis=0)
    mono = mono - mono.mean()
    decoded_s = x.shape[1] / sr
    lufs, lra, tp = loudness(path)

    peak = float(np.abs(x).max())
    rms = float(np.sqrt(np.mean(mono.astype(np.float64) ** 2)))
    rms_db = frame_rms_db(mono, sr)
    gaps = _gaps(rms_db)
    live = rms_db[rms_db > -100]
    # The floor is the quiet end of the gaps; its flatness is judged only on frames near
    # it, so a tonal decay tail passing through a gap can't make broadband noise look tonal.
    floor_db = float(np.percentile(rms_db[gaps], 20)) if gaps.sum() >= 10 else None
    snr = float(np.percentile(live, 95) - floor_db) if floor_db is not None and live.size else None
    flat = _gap_flatness(mono, sr, gaps & (rms_db <= (floor_db or -200.0) + 3.0)) if floor_db is not None else None
    lead, trail = _edge_silence(rms_db)
    freqs, pdb = spectrum_db(mono, sr)
    shares = band_shares(freqs, pdb)
    bw = _bandwidth(freqs, pdb)
    hum_hz, hum_prom = _hum(mono, sr)
    on = onsets(mono, sr)
    events, clipped = _clip_events(x)
    clicks = _clicks(mono, sr, on)
    p = 10 ** (pdb / 10)
    centroid = float((freqs * p).sum() / p.sum()) if p.sum() > 0 else 0.0
    corr = side_db = None
    if nch >= 2:
        left, right = x[0].astype(np.float64), x[1].astype(np.float64)
        denom = math.sqrt(float((left**2).sum() * (right**2).sum())) or 1.0
        corr = round(float((left * right).sum() / denom), 4)
        mid, side = (left + right) / 2, (left - right) / 2
        side_db = round(_db(float(np.sqrt(np.mean(side**2)))) - _db(float(np.sqrt(np.mean(mid**2)))), 2)

    crest = _db(peak) - _db(rms)
    gaps_frac = float(gaps.mean()) if gaps.size else 0.0
    if category == "auto":
        cat, conf, why = classify(decoded_s, len(on), shares, gaps_frac, crest)
    else:
        cat, conf, why = category, "high", "set by you"

    rep = AudioReport(
        path=str(path), container=info["container"], codec=info["codec"],
        lossy=info["codec"] in LOSSY_CODECS, bit_rate=info["bit_rate"], sample_rate=sr, channels=nch,
        duration_s=round(info["duration_s"], 4), decoded_duration_s=round(decoded_s, 4),
        lufs=lufs, lra=lra, true_peak_db=tp, peak_db=round(_db(peak), 2), rms_db=round(_db(rms), 2),
        crest_db=round(crest, 2), dc_offset=[round(float(c.mean()), 6) for c in x],
        clip_events=events, clipped_samples=clipped, noise_floor_db=None if floor_db is None else round(floor_db, 2),
        gap_flatness=flat, snr_db=None if snr is None else round(snr, 2), lead_silence_s=lead,
        trail_silence_s=trail, bandwidth_hz=round(bw, 1), hum_hz=hum_hz, hum_prominence_db=hum_prom,
        clicks=clicks, onsets_s=on, stereo_correlation=corr, side_ratio_db=side_db, band_shares=shares,
        spectral_centroid_hz=round(centroid, 1), category=cat, category_confidence=conf, category_reason=why,
        short_clip=decoded_s < SHORT_CLIP_S,
    )
    rep.findings = findings(rep)
    return rep


def findings(r: AudioReport) -> list[Finding]:
    out: list[Finding] = []
    if r.clip_events:
        sev = "major" if r.clip_events >= 10 or r.clipped_samples > r.sample_rate * 0.001 else "minor"
        out.append(Finding("clipping", sev, "Distorted peaks (clipping)",
                           f"{r.clip_events} clipped run(s), {r.clipped_samples} samples at full scale",
                           "declip"))
    dc = max((abs(d) for d in r.dc_offset), default=0.0)
    if dc >= DC_MINOR:
        out.append(Finding("dc_offset", "major" if dc >= DC_MAJOR else "minor", "Off-centre waveform (DC offset)",
                           f"mean offset {dc:.4f}", "dc"))
    if math.isfinite(r.true_peak_db) and r.true_peak_db > TOO_HOT_TP and not r.clip_events:
        out.append(Finding("too_hot", "minor", "Peaks touch the ceiling",
                           f"true peak {r.true_peak_db:+.1f} dBTP", "limit"))
    if math.isfinite(r.lufs) and r.lufs < QUIET_LUFS:
        out.append(Finding("very_quiet", "minor", "Very quiet overall", f"{r.lufs:.1f} LUFS integrated", "gain"))
    if (
        r.snr_db is not None and r.gap_flatness is not None and r.gap_flatness >= NOISE_FLATNESS
        and r.snr_db < NOISE_SNR_MINOR
    ):
        sev = "major" if r.snr_db < NOISE_SNR_MAJOR else "minor"
        out.append(Finding("noise", sev, "Background hiss or noise between sounds",
                           f"gaps sit {r.snr_db:.0f} dB below the loud parts; gap flatness {r.gap_flatness:.2f} "
                           "(broadband, not tonal)", "denoise",
                           "medium" if r.category in ("sfx", "music", "ambience") else "high"))
    if r.hum_hz and r.hum_prominence_db is not None and r.hum_prominence_db >= HUM_PROMINENCE:
        out.append(Finding("hum", "minor", f"Mains hum ({r.hum_hz} Hz)",
                           f"{r.hum_hz} Hz peak {r.hum_prominence_db:.0f} dB above its neighbours", "dehum",
                           "medium" if r.category == "music" else "high"))
    if r.clicks >= CLICKS_MINOR:
        out.append(Finding("clicks", "minor", "Clicks or crackles", f"{r.clicks} isolated spike(s) under 1 ms",
                           "declick", "low" if r.category == "sfx" else "medium"))
    if r.lead_silence_s > EDGE_SILENCE_MIN_S or r.trail_silence_s > EDGE_SILENCE_MIN_S:
        out.append(Finding("edge_silence", "info", "Silence at the start or end",
                           f"{r.lead_silence_s:.2f}s before, {r.trail_silence_s:.2f}s after (below {SILENCE_DB:.0f} dB)",
                           "trim"))
    if r.stereo_correlation is not None and r.stereo_correlation < 0:
        out.append(Finding("phase", "minor", "Left and right partly cancel (phase)",
                           f"channel correlation {r.stereo_correlation:+.2f}", None, "medium"))
    if r.stereo_correlation is not None and r.stereo_correlation > 0.999:
        out.append(Finding("dual_mono", "info", "Stereo file with identical channels",
                           f"correlation {r.stereo_correlation:.4f}", None))
    nyq = r.sample_rate / 2
    if r.bandwidth_hz and r.bandwidth_hz < NARROW_BAND_HZ and nyq > NARROW_BAND_HZ * 1.3:
        out.append(Finding("narrow_band", "info", "Dull or band-limited (missing highs)",
                           f"content stops near {r.bandwidth_hz / 1000:.1f} kHz of {nyq / 1000:.1f} kHz possible",
                           None, "medium"))
    if r.lossy:
        out.append(Finding("lossy_source", "info", "Lossy source",
                           f"{r.codec} at {round((r.bit_rate or 0) / 1000)} kbps — exports cannot restore what the "
                           "encoder discarded", None))
    if r.short_clip:
        out.append(Finding("short_clip", "info", "Short clip",
                           f"{r.decoded_duration_s:.2f}s — loudness is judged by peak and RMS, not gated LUFS",
                           None))
    return out


def peaks(path: Path | str, points: int = 600) -> dict[str, Any]:
    """Min/max envelope for drawing a waveform: ``points`` buckets per channel-mixed signal."""
    path = Path(path)
    info = probe(path)
    if info["duration_s"] > MAX_DURATION_S:
        raise AnalyzeError(f"{path.name} is longer than {int(MAX_DURATION_S // 60)} minutes")
    x = decode(path, info["sample_rate"], info["channels"])
    mono = x.mean(axis=0)
    points = max(1, min(points, mono.size))
    edges = np.linspace(0, mono.size, points + 1).astype(int)
    lo = [round(float(mono[a:b].min()), 4) if b > a else 0.0 for a, b in zip(edges[:-1], edges[1:])]
    hi = [round(float(mono[a:b].max()), 4) if b > a else 0.0 for a, b in zip(edges[:-1], edges[1:])]
    return {"points": points, "duration_s": round(mono.size / info["sample_rate"], 4),
            "sample_rate": info["sample_rate"], "channels": info["channels"], "min": lo, "max": hi}
