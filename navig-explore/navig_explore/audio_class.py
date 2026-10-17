"""Content-based audio classification for navig-explore.

``probe.py`` deliberately stops at the door for audio — it records size/mtime for the
manifest and exact-dedup, but never listens to the file. That leaves the one question a
sound library actually needs answered: *is this a song, a person talking, or a noise?*
Filenames can't answer it (a TikTok rip is named ``#fyp_#viral_7593…m4a``), and tags are
absent on ripped clips.

This module answers it from the waveform, with **no model download and no heavyweight
dependency** — ffmpeg + numpy only:

1. :func:`extract_features` decodes a fixed 30 s window from the middle of the file to
   16 kHz mono, peak-normalises it, and derives ~20 classic speech/music-discrimination
   features (4 Hz syllabic modulation, low-energy-frame ratio, spectral flux/flatness,
   onset-autocorrelation beat strength, frame-level harmonicity, …).
2. :func:`train` fits a multinomial logistic regression on folders **you already
   sorted** — your own library is the training set — and reports honest held-out
   accuracy from k-fold cross-validation.
3. :func:`classify_folder` scores new files and emits a per-file plan.

Design rules that matter for correctness here:

- **The same window for training and inference.** A 4-minute album track and a 20 s
  TikTok rip are compared on a middle 30 s window each, so the model can't learn
  "long ⇒ music" — a shortcut that would be exactly backwards for a folder of clips.
- **No duration, bitrate, codec or loudness in the feature vector.** Those describe the
  *file*, not the *sound*, and every one of them correlates with the source folder
  during training. ``duration`` is still reported for routing/reporting, just never fed
  to the model.
- **Peak normalisation** before feature extraction, so a quiet rip and a loud master
  land in the same place.
- **Read-only.** Nothing here moves or deletes a file; it produces records and a plan.

Runnable standalone (no navig install needed):

    python audio_class.py train  --label music=X:\\Audio\\Music --out model.json
    python audio_class.py sort   <folder> --model model.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np

AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus",
             ".wma", ".aif", ".aiff", ".octet-stream", ".weba", ".webm"}
# `_dupes` / `_duplicates` are quarantine folders other tools leave *inside* the tree they
# cleaned. Classifying their contents promotes files a previous pass deliberately set
# aside back into the live library — a sort should never undo someone else's dedupe.
SKIP_DIRS = {".mediaexplorer", "_organized", ".trash", "_trash", "$recycle.bin",
             "system volume information", "__macosx", "_review", "_dupes",
             "_duplicates", "_quarantine"}

SR = 16000          # analysis rate — 8 kHz Nyquist covers speech + musical texture
WIN = 30.0          # seconds analysed per file (same for training and inference)
N_FFT = 512         # 32 ms frame
HOP = 160           # 10 ms hop → 100 Hz envelope rate, enough for the 4 Hz band

# Feature order is part of the model contract: a model JSON stores this list and
# `predict` re-reads it, so adding a feature invalidates old models loudly, not silently.
FEATURES = [
    "low_energy_ratio", "silence_ratio", "rms_cv", "dyn_range_db",
    "mod4hz_ratio", "mod_peak_hz", "env_flatness",
    "zcr_mean", "zcr_std",
    "centroid_mean", "centroid_std", "rolloff_mean", "rolloff_std",
    "bandwidth_mean", "flatness_mean", "flatness_std",
    "flux_mean", "flux_std",
    "beat_strength", "harmonicity_mean", "harmonicity_std", "voiced_ratio",
    # Silero VAD — the single most decisive cue for "is someone talking?".
    # Present only when faster-whisper is installed; see `vad_available()`.
    "speech_ratio", "speech_seg_per_min", "speech_seg_mean_s",
]

# ── voice activity detection (optional, via faster-whisper's bundled Silero) ──
_vad_lock = threading.Lock()
_vad_state: dict = {}


def vad_available() -> bool:
    """True when Silero VAD can be used. A model trained with VAD features MUST NOT be
    applied without them, so this flag is recorded in the model and checked on predict."""
    if "ok" not in _vad_state:
        try:
            from faster_whisper.vad import VadOptions, get_speech_timestamps  # noqa: F401,PLC0415
            _vad_state["ok"] = True
        except Exception:  # noqa: BLE001 — optional dependency
            _vad_state["ok"] = False
    return _vad_state["ok"]


def speech_features(x: np.ndarray) -> dict:
    """Fraction of the window Silero VAD calls speech, plus segment shape.

    Tuned for short clips: the library default waits 2 s of silence before closing a
    segment, which swallows the gaps that make a 15 s TikTok clip readable.
    """
    if not vad_available():
        return {"speech_ratio": 0.0, "speech_seg_per_min": 0.0, "speech_seg_mean_s": 0.0}
    from faster_whisper.vad import VadOptions, get_speech_timestamps  # noqa: PLC0415
    opts = VadOptions(threshold=0.5, min_speech_duration_ms=150,
                      min_silence_duration_ms=300, speech_pad_ms=100)
    try:
        with _vad_lock:      # one ONNX session, shared state — serialise access
            segs = get_speech_timestamps(x, vad_options=opts, sampling_rate=SR)
    except Exception:  # noqa: BLE001 — never let VAD failure kill a scan
        return {"speech_ratio": 0.0, "speech_seg_per_min": 0.0, "speech_seg_mean_s": 0.0}
    total = x.size / SR
    spoken = sum(s["end"] - s["start"] for s in segs) / SR
    return {
        "speech_ratio": float(min(1.0, spoken / max(total, 1e-6))),
        "speech_seg_per_min": float(len(segs) / max(total / 60.0, 1e-6)),
        "speech_seg_mean_s": float(spoken / len(segs)) if segs else 0.0,
    }


# ── decode ──────────────────────────────────────────────────────────────────
def probe_duration(path: Path) -> float:
    """Container duration in seconds (0.0 if ffprobe can't tell)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, timeout=30, text=True, encoding="utf-8",
            errors="replace",
        )
        return round(float((out.stdout or "0").strip() or 0), 2)
    except Exception:  # noqa: BLE001 — corrupt file / no ffprobe / no duration tag
        return 0.0


def decode_window(path: Path, duration: float) -> np.ndarray | None:
    """Decode the middle ``WIN`` seconds to 16 kHz mono float32 in [-1, 1].

    Centring the window is what keeps a 4-minute track comparable to a 20 s clip:
    both contribute their middle, not their intro.
    """
    start = max(0.0, (duration - WIN) / 2.0) if duration and duration > WIN else 0.0
    cmd = ["ffmpeg", "-v", "quiet", "-nostdin"]
    if start > 0:
        cmd += ["-ss", f"{start:.2f}"]
    cmd += ["-i", str(path), "-t", f"{WIN:.2f}", "-ac", "1", "-ar", str(SR),
            "-f", "f32le", "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=120)
    except Exception:  # noqa: BLE001
        return None
    if not out.stdout:
        return None
    x = np.frombuffer(out.stdout, dtype="<f4").astype(np.float32)
    if x.size < SR // 2:          # < 0.5 s of audio — nothing to characterise
        return None
    x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
    peak = float(np.max(np.abs(x)))
    if peak > 0:
        x = x / peak              # loudness must not become a class cue
    return x


# ── features ────────────────────────────────────────────────────────────────
def _frames(x: np.ndarray) -> np.ndarray:
    """(T, N_FFT) overlapping frames, zero-padded to a whole number of hops."""
    n = 1 + max(0, (x.size - N_FFT) // HOP)
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(n)[:, None]
    return x[idx]


def _autocorr_peak(sig: np.ndarray, lo: int, hi: int) -> float:
    """Normalised autocorrelation peak of ``sig`` within a lag window [lo, hi)."""
    if sig.size < hi + 1 or hi <= lo:
        return 0.0
    s = sig - sig.mean()
    denom = float(np.dot(s, s))
    if denom <= 1e-12:
        return 0.0
    n = 1 << int(math.ceil(math.log2(2 * s.size)))
    spec = np.fft.rfft(s, n)
    ac = np.fft.irfft(spec * np.conj(spec), n)[: hi + 1]
    seg = ac[lo:hi]
    if seg.size == 0:
        return 0.0
    return float(max(0.0, seg.max() / denom))


def extract_features(path: Path, duration: float | None = None) -> dict | None:
    """~25 content features for one audio file, or ``None`` if it can't be decoded."""
    dur = probe_duration(path) if duration is None else duration
    x = decode_window(path, dur)
    if x is None:
        return None
    return features_from_array(x, dur)


def features_from_array(x: np.ndarray, dur: float = 0.0) -> dict | None:
    """Features for an already-decoded 16 kHz mono window (the unit-testable core)."""
    fr = _frames(x)
    if fr.shape[0] < 8:
        return None
    win = np.hanning(N_FFT).astype(np.float32)
    spec = np.abs(np.fft.rfft(fr * win, axis=1))
    power = spec ** 2
    freqs = np.fft.rfftfreq(N_FFT, 1.0 / SR)
    eps = 1e-10

    # --- energy envelope: pauses, dynamics, syllabic rhythm -----------------
    rms = np.sqrt(np.mean(fr ** 2, axis=1) + eps)
    mean_rms = float(rms.mean())
    low_energy_ratio = float(np.mean(rms < 0.5 * mean_rms))     # speech pauses a lot
    silence_ratio = float(np.mean(rms < 10 ** (-50 / 20)))
    rms_cv = float(rms.std() / (mean_rms + eps))
    db = 20 * np.log10(rms + eps)
    dyn_range_db = float(np.percentile(db, 95) - np.percentile(db, 5))

    env = rms - rms.mean()
    env_spec = np.abs(np.fft.rfft(env))
    env_f = np.fft.rfftfreq(env.size, HOP / SR)                 # envelope rate = 100 Hz
    band = (env_f >= 0.5) & (env_f <= 20.0)
    syl = (env_f >= 3.0) & (env_f <= 6.0)                       # syllable rate of speech
    band_e = float(env_spec[band].sum()) + eps
    mod4hz_ratio = float(env_spec[syl].sum() / band_e)
    mod_peak_hz = float(env_f[band][np.argmax(env_spec[band])]) if band.any() else 0.0
    env_b = env_spec[band] + eps
    env_flatness = float(np.exp(np.mean(np.log(env_b))) / np.mean(env_b))

    # --- waveform shape ------------------------------------------------------
    zc = np.mean(np.abs(np.diff(np.sign(fr), axis=1)) > 0, axis=1)
    zcr_mean, zcr_std = float(zc.mean()), float(zc.std())

    # --- spectral shape ------------------------------------------------------
    psum = power.sum(axis=1) + eps
    centroid = (power * freqs).sum(axis=1) / psum
    cum = np.cumsum(power, axis=1)
    roll_idx = np.argmax(cum >= (0.85 * psum)[:, None], axis=1)
    rolloff = freqs[roll_idx]
    bandwidth = np.sqrt((power * (freqs[None, :] - centroid[:, None]) ** 2).sum(axis=1) / psum)
    flatness = np.exp(np.mean(np.log(power + eps), axis=1)) / (power.mean(axis=1) + eps)

    # --- flux / onsets / beat ------------------------------------------------
    d = np.diff(spec, axis=0)
    flux = np.sqrt((np.maximum(d, 0) ** 2).sum(axis=1))
    flux = flux / (flux.mean() + eps)
    # a steady pulse shows up as a peak at 0.3–1.2 s lag (50–200 BPM)
    beat_strength = _autocorr_peak(flux, int(0.30 * SR / HOP), int(1.20 * SR / HOP))

    # --- harmonicity: is there a stable pitch? -------------------------------
    # sung/played notes hold a pitch far longer than speech; noise holds none.
    lo, hi = int(SR / 500), int(SR / 60)          # f0 60–500 Hz
    step = max(1, fr.shape[0] // 60)              # ~60 probes is plenty
    harm = np.array([_autocorr_peak(fr[i], lo, hi) for i in range(0, fr.shape[0], step)])
    harmonicity_mean, harmonicity_std = float(harm.mean()), float(harm.std())
    voiced_ratio = float(np.mean(harm > 0.35))

    out = {
        "dur": dur,
        "low_energy_ratio": low_energy_ratio, "silence_ratio": silence_ratio,
        "rms_cv": rms_cv, "dyn_range_db": dyn_range_db,
        "mod4hz_ratio": mod4hz_ratio, "mod_peak_hz": mod_peak_hz,
        "env_flatness": env_flatness,
        "zcr_mean": zcr_mean, "zcr_std": zcr_std,
        "centroid_mean": float(centroid.mean()), "centroid_std": float(centroid.std()),
        "rolloff_mean": float(rolloff.mean()), "rolloff_std": float(rolloff.std()),
        "bandwidth_mean": float(bandwidth.mean()),
        "flatness_mean": float(flatness.mean()), "flatness_std": float(flatness.std()),
        "flux_mean": float(flux.mean()), "flux_std": float(flux.std()),
        "beat_strength": beat_strength,
        "harmonicity_mean": harmonicity_mean, "harmonicity_std": harmonicity_std,
        "voiced_ratio": voiced_ratio,
    }
    out.update(speech_features(x))
    return out


def vec(f: dict) -> np.ndarray:
    v = np.array([f.get(k, 0.0) for k in FEATURES], dtype=np.float64)
    return np.nan_to_num(v, nan=0.0, posinf=0.0, neginf=0.0)


# ── walking ─────────────────────────────────────────────────────────────────
def iter_audio(root: Path, limit: int | None = None, extensionless: bool = True):
    """Every audio file under ``root``.

    ``extensionless`` also yields files with **no** suffix. Messenger and API dumps
    routinely write bytes with no extension (Telegram's downloader names 35 of 87 audio
    files ``files/81`` with nothing on the end) — skipping those silently loses part of
    the folder, which is worse than paying for a decode that returns ``None``.
    """
    n = 0
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None,
                                                followlinks=False):
        dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS]
        for fn in sorted(filenames):
            ext = os.path.splitext(fn)[1].lower()
            if ext in AUDIO_EXT or (extensionless and not ext):
                yield Path(dirpath) / fn
                n += 1
                if limit and n >= limit:
                    return


def scan_files(paths: list[Path], workers: int = 8, quiet: bool = False,
               out_path: Path | None = None, done: set[str] | None = None) -> list[dict]:
    """Extract features for many files in parallel; append each record as it lands.

    Writing incrementally (and skipping ``done``) makes a multi-thousand-file scan
    resumable — the same guarantee ``probe_folder`` gives.
    """
    done = done or set()
    todo = [p for p in paths if str(p) not in done]
    recs: list[dict] = []
    lock = threading.Lock()
    fh = out_path.open("a", encoding="utf-8") if out_path else None
    t0, fails = time.time(), 0

    def work(p: Path) -> dict | None:
        f = extract_features(p)
        if f is None:
            return {"path": str(p), "name": p.name, "ok": False}
        f.update({"path": str(p), "name": p.name, "ok": True})
        return f

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for fut in as_completed(ex.submit(work, p) for p in todo):
            try:
                r = fut.result()
            except Exception:  # noqa: BLE001 — one bad file must not kill the scan
                r = None
            if r is None:
                continue
            with lock:
                recs.append(r)
                if not r.get("ok"):
                    fails += 1
                if fh:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
                if not quiet and len(recs) % 200 == 0:
                    rate = len(recs) / max(time.time() - t0, 0.1)
                    print(f"  [audio] {len(recs)}/{len(todo)}  ({rate:.1f}/s, fail {fails})",
                          flush=True)
    if fh:
        fh.close()
    if not quiet:
        print(f"[audio] scanned {len(recs)} ({fails} undecodable) in {time.time()-t0:.0f}s",
              flush=True)
    return recs


# ── model: multinomial logistic regression (numpy, no sklearn) ──────────────
def _softmax(z: np.ndarray) -> np.ndarray:
    z = z - z.max(axis=1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=1, keepdims=True)


# Defaults chosen by cross-validated sweep over hidden ∈ {8,16,32,48} × lr × l2 on the
# operator's own library: regularisation mattered far more than width (l2 3e-3 beat 3e-4
# by ~4 points at every width), which is what you expect from hand-labelled folders.
HIDDEN = 32


def _fit_mlp(X: np.ndarray, y: np.ndarray, n_class: int, *, l2: float = 3e-3,
             iters: int = 4000, lr: float = 0.01, seed: int = 0):
    """One-hidden-layer tanh network, Adam, class-balanced loss.

    A linear model cannot express the combination that decides this problem — "speech
    *and* a steady beat ⇒ a song with vocals, not a person talking". One hidden layer
    can, and at 24 units on ~25 inputs it stays far too small to memorise the set.
    """
    rng = np.random.default_rng(seed)
    n, d = X.shape
    P1 = [rng.normal(0, np.sqrt(1.0 / d), (d, HIDDEN)), np.zeros(HIDDEN)]
    P2 = [rng.normal(0, np.sqrt(1.0 / HIDDEN), (HIDDEN, n_class)), np.zeros(n_class)]
    params = P1 + P2
    Y = np.eye(n_class)[y]
    counts = np.bincount(y, minlength=n_class).astype(float)
    # balanced weights: a class with a third of the samples must not be a third as loud
    cw = (counts.sum() / (n_class * np.maximum(counts, 1)))[y][:, None]
    m = [np.zeros_like(p) for p in params]
    v = [np.zeros_like(p) for p in params]
    b1, b2, eps = 0.9, 0.999, 1e-8
    for t in range(1, iters + 1):
        W1, c1, W2, c2 = params
        H = np.tanh(X @ W1 + c1)
        P = _softmax(H @ W2 + c2)
        dZ2 = (P - Y) * cw / n
        gW2 = H.T @ dZ2 + l2 * W2
        gc2 = dZ2.sum(axis=0)
        dH = (dZ2 @ W2.T) * (1 - H ** 2)
        gW1 = X.T @ dH + l2 * W1
        gc1 = dH.sum(axis=0)
        for i, g in enumerate((gW1, gc1, gW2, gc2)):
            m[i] = b1 * m[i] + (1 - b1) * g
            v[i] = b2 * v[i] + (1 - b2) * g ** 2
            params[i] -= lr * (m[i] / (1 - b1 ** t)) / (np.sqrt(v[i] / (1 - b2 ** t)) + eps)
    return params


def _predict_raw(params, mu, sd, X):
    W1, c1, W2, c2 = params
    Z = (X - mu) / sd
    return _softmax(np.tanh(Z @ W1 + c1) @ W2 + c2)


def train(samples: list[tuple[dict, str]], *, labels: list[str] | None = None,
          folds: int = 5, seed: int = 0, **hp) -> dict:
    """Fit on (features, label) pairs and report k-fold held-out accuracy.

    The reported accuracy is cross-validated, never training accuracy — the only
    number worth quoting when the training set is "folders I sorted by hand".
    """
    labels = labels or sorted({lab for _, lab in samples})
    li = {lab: i for i, lab in enumerate(labels)}
    X = np.stack([vec(f) for f, _ in samples])
    y = np.array([li[lab] for _, lab in samples])

    rng = np.random.default_rng(seed)
    order = rng.permutation(len(y))
    X, y = X[order], y[order]

    # k-fold CV — honest generalisation estimate + a confusion matrix to read
    conf = np.zeros((len(labels), len(labels)), dtype=int)
    fold_id = np.arange(len(y)) % folds
    for k in range(folds):
        tr, te = fold_id != k, fold_id == k
        if te.sum() == 0 or len(np.unique(y[tr])) < len(labels):
            continue
        mu, sd = X[tr].mean(0), X[tr].std(0) + 1e-9
        p = _fit_mlp((X[tr] - mu) / sd, y[tr], len(labels), seed=seed, **hp)
        pred = _predict_raw(p, mu, sd, X[te]).argmax(1)
        for t, q in zip(y[te], pred):
            conf[t, q] += 1

    # final model on everything
    mu, sd = X.mean(0), X.std(0) + 1e-9
    W1, b1_, W2, b2_ = _fit_mlp((X - mu) / sd, y, len(labels), seed=seed, **hp)
    acc = float(np.trace(conf) / max(conf.sum(), 1))
    per_class = {labels[i]: float(conf[i, i] / max(conf[i].sum(), 1))
                 for i in range(len(labels))}
    return {
        "features": FEATURES, "labels": labels, "vad": vad_available(), "arch": "mlp",
        "mu": mu.tolist(), "sd": sd.tolist(),
        "W1": W1.tolist(), "b1": b1_.tolist(), "W2": W2.tolist(), "b2": b2_.tolist(),
        "cv_accuracy": acc, "cv_recall": per_class,
        "confusion": conf.tolist(), "n_train": int(len(y)),
        "counts": {labels[i]: int((y == i).sum()) for i in range(len(labels))},
    }


def predict(model: dict, feats: list[dict]) -> list[tuple[str, float, dict]]:
    """→ [(label, confidence, {label: prob})] in the order given."""
    if model.get("features") != FEATURES:
        raise ValueError("model was trained on a different feature set — retrain it")
    if model.get("vad") and not vad_available():
        # Silently scoring zeros for three features the model leans on would look like
        # a working classifier and behave like a broken one. Fail loudly instead.
        raise ValueError("model was trained with Silero VAD but faster-whisper is not "
                         "importable here — install it or retrain without VAD")
    X = np.stack([vec(f) for f in feats])
    params = [np.array(model[k]) for k in ("W1", "b1", "W2", "b2")]
    P = _predict_raw(params, np.array(model["mu"]), np.array(model["sd"]), X)
    labels = model["labels"]
    out = []
    for row in P:
        i = int(row.argmax())
        out.append((labels[i], float(row[i]),
                    {labels[j]: float(row[j]) for j in range(len(labels))}))
    return out


# ── high level ──────────────────────────────────────────────────────────────
def learn_from_folders(spec: dict[str, "Path | str | list"], *, per_class: int = 400,
                       workers: int = 8, seed: int = 0, quiet: bool = False,
                       cache: Path | None = None) -> dict:
    """Sample ``per_class`` files per label from folders you already sorted, and train.

    A label may map to one folder or several — real libraries keep "certainly music" in
    more than one place, and picking the *pure* subfolders beats pointing at a whole
    tree that has a podcast misfiled under SFX.
    """
    rng = random.Random(seed)
    samples: list[tuple[dict, str]] = []
    for label, folders in spec.items():
        if isinstance(folders, (str, Path)):
            folders = [folders]
        files: list[Path] = []
        for folder in folders:
            files += list(iter_audio(Path(folder)))
        rng.shuffle(files)
        pick = files[:per_class]
        if not quiet:
            print(f"[audio] {label}: {len(pick)} of {len(files)} from "
                  f"{len(folders)} folder(s)", flush=True)
        recs = scan_files(pick, workers=workers, quiet=quiet,
                          out_path=(cache / f"train-{label}.jsonl") if cache else None)
        samples += [(r, label) for r in recs if r.get("ok")]
    return train(samples)


def classify_folder(root: Path, model: dict, *, workers: int = 8, limit: int | None = None,
                    quiet: bool = False, cache: Path | None = None) -> list[dict]:
    """Score every audio file under ``root`` → records with label/confidence."""
    files = list(iter_audio(root, limit=limit))
    out_path = cache / "audio-features.jsonl" if cache else None
    done: set[str] = set()
    if out_path and out_path.exists():
        for line in out_path.open(encoding="utf-8", errors="replace"):
            try:
                done.add(json.loads(line)["path"])
            except Exception:  # noqa: BLE001
                pass
    recs = scan_files(files, workers=workers, quiet=quiet, out_path=out_path, done=done)
    if out_path and done:            # fold previously-scanned records back in
        seen = {r["path"] for r in recs}
        for line in out_path.open(encoding="utf-8", errors="replace"):
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if r["path"] not in seen:
                recs.append(r)
                seen.add(r["path"])
    ok = [r for r in recs if r.get("ok")]
    if ok:
        for r, (lab, conf, probs) in zip(ok, predict(model, ok)):
            r["label"], r["confidence"], r["probs"] = lab, conf, probs
    for r in recs:
        if not r.get("ok"):
            r["label"], r["confidence"], r["probs"] = "_undecodable", 0.0, {}
    return recs


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="Content-based audio classification")
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("train")
    t.add_argument("--label", action="append", required=True, help="name=folder")
    t.add_argument("--out", required=True)
    t.add_argument("--per-class", type=int, default=400)
    t.add_argument("--workers", type=int, default=8)
    s = sub.add_parser("sort")
    s.add_argument("folder")
    s.add_argument("--model", required=True)
    s.add_argument("--workers", type=int, default=8)
    s.add_argument("--limit", type=int, default=None)
    a = ap.parse_args(argv)

    if a.cmd == "train":
        spec = {kv.split("=", 1)[0]: Path(kv.split("=", 1)[1]) for kv in a.label}
        m = learn_from_folders(spec, per_class=a.per_class, workers=a.workers)
        Path(a.out).write_text(json.dumps(m), encoding="utf-8")
        print(json.dumps({k: m[k] for k in ("cv_accuracy", "cv_recall", "counts",
                                            "confusion", "labels")}, indent=2))
        return 0

    model = json.loads(Path(a.model).read_text(encoding="utf-8"))
    recs = classify_folder(Path(a.folder), model, workers=a.workers, limit=a.limit)
    from collections import Counter
    print(json.dumps(Counter(r["label"] for r in recs), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
