"""Sound-alike search: "find sounds like this one" across a folder — local, cached.

Each file gets a small timbre + rhythm fingerprint from its first 30 s at 16 kHz mono:
the mean and spread of 32 log-mel bands, spectral centroid / roll-off / flatness, and
onset rate. Files are compared by cosine similarity of standardised fingerprints.
This finds sounds that *sound alike* (two different door slams, two dark drones);
exact or near-duplicate copies are a different job (chromaprint, `navig media dedupe-audio`).

The index lives in ``<data dir>/audio-similar/<folder hash>.npz`` and is refreshed per
file when its size or modification time changes.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

SR = 16000
CLIP_S = 30.0
N_MELS = 32
AUDIO_EXTS = {".wav", ".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".aif", ".aiff", ".wma"}
MAX_FILES = 20000
WORKERS = 4


class SimilarError(RuntimeError):
    """Unreadable query, empty folder, or ffmpeg missing."""


@dataclass
class Match:
    path: str
    score: float  # cosine similarity of standardised fingerprints, 1.0 = identical character

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "score": round(self.score, 4)}


def _decode(path: Path) -> np.ndarray:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise SimilarError("ffmpeg is not on PATH")
    proc = subprocess.run([exe, "-hide_banner", "-nostdin", "-v", "error", "-t", str(CLIP_S), "-i", str(path),  # noqa: S603
                           "-vn", "-f", "f32le", "-ac", "1", "-ar", str(SR), "-"], capture_output=True, timeout=120, check=False)
    if proc.returncode != 0:
        raise SimilarError(f"could not decode {path.name}")
    return np.frombuffer(proc.stdout, dtype="<f4").astype(np.float64)


def _mel_filters(n_fft: int) -> np.ndarray:
    def hz2mel(f): return 2595 * np.log10(1 + f / 700)
    def mel2hz(m): return 700 * (10 ** (m / 2595) - 1)

    mels = np.linspace(hz2mel(40), hz2mel(SR / 2 - 200), N_MELS + 2)
    bins = np.floor((n_fft + 1) * mel2hz(mels) / SR).astype(int)
    fb = np.zeros((N_MELS, n_fft // 2 + 1))
    for m in range(1, N_MELS + 1):
        a, b, c = bins[m - 1], bins[m], bins[m + 1]
        if b > a:
            fb[m - 1, a:b] = (np.arange(a, b) - a) / (b - a)
        if c > b:
            fb[m - 1, b:c] = (c - np.arange(b, c)) / (c - b)
    return fb


_FB = _mel_filters(1024)


def fingerprint(path: Path) -> np.ndarray:
    x = _decode(path)
    if x.size < SR // 4 or not np.any(x):
        raise SimilarError(f"{path.name} is too short or silent to fingerprint")
    x = x / (np.abs(x).max() + 1e-9)
    n_fft, hop = 1024, 256
    count = 1 + max(0, (x.size - n_fft) // hop)
    idx = np.arange(n_fft)[None, :] + hop * np.arange(count)[:, None]
    frames = x[np.minimum(idx, x.size - 1)] * np.hanning(n_fft)
    spec = np.abs(np.fft.rfft(frames, axis=1)) ** 2 + 1e-12
    mel = np.log(spec @ _FB.T + 1e-10)
    freqs = np.fft.rfftfreq(n_fft, 1 / SR)
    centroid = (spec * freqs).sum(1) / spec.sum(1)
    cum = np.cumsum(spec, axis=1)
    rolloff = freqs[np.argmax(cum >= 0.85 * cum[:, -1:], axis=1)]
    flat = np.exp(np.mean(np.log(spec), axis=1)) / np.mean(spec, axis=1)
    flux = np.maximum(0, np.diff(mel, axis=0)).sum(1)
    onset_rate = float((flux > np.percentile(flux, 90)).mean()) if flux.size else 0.0
    return np.concatenate([mel.mean(0), mel.std(0),
                           [np.log(centroid.mean() + 1), np.log(rolloff.mean() + 1), flat.mean(), flat.std(),
                            onset_rate, np.log(x.size / SR + 1)]])


def _index_path(folder: Path) -> Path:
    from navig.platform import paths

    key = hashlib.sha1(str(folder.resolve()).lower().encode()).hexdigest()[:16]  # noqa: S324 — a cache key, not security
    d = paths.data_dir() / "audio-similar"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{key}.npz"


def build_index(folder: Path | str, *, reindex: bool = False) -> tuple[list[str], np.ndarray, int]:
    """(paths, vectors, how many were (re)computed) — cached, refreshed per changed file."""
    root = Path(folder)
    if not root.is_dir():
        raise SimilarError(f"no such folder: {root}")
    files = [p for p in root.rglob("*") if p.suffix.lower() in AUDIO_EXTS and p.is_file()][:MAX_FILES]
    if not files:
        raise SimilarError(f"no audio files in {root}")
    cache_file = _index_path(root)
    cache: dict[str, tuple[str, np.ndarray]] = {}
    if cache_file.exists() and not reindex:
        try:
            z = np.load(cache_file, allow_pickle=False)
            meta = json.loads(str(z["meta"]))
            for i, m in enumerate(meta):
                cache[m["path"]] = (m["stamp"], z["vecs"][i])
        except (OSError, ValueError, KeyError):
            cache = {}
    from concurrent.futures import ThreadPoolExecutor

    def stamp_of(f: Path) -> str:
        st = f.stat()
        return f"{st.st_size}:{int(st.st_mtime)}"

    stamps = {f: stamp_of(f) for f in files}
    todo = [f for f in files if not (str(f) in cache and cache[str(f)][0] == stamps[f])]

    def safe_fp(f: Path) -> np.ndarray | None:
        try:
            return fingerprint(f)
        except SimilarError:
            return None

    # ffmpeg decodes outside the GIL, so a few threads fingerprint a big folder several times faster.
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        fresh = dict(zip(todo, pool.map(safe_fp, todo)))
    paths, vecs, kept_stamps = [], [], []
    for f in files:
        v = fresh[f] if f in fresh else cache[str(f)][1]
        if v is None:
            continue
        paths.append(str(f))
        vecs.append(v)
        kept_stamps.append(stamps[f])
    computed = sum(1 for v in fresh.values() if v is not None)
    if not vecs:
        raise SimilarError("none of the files could be fingerprinted")
    arr = np.vstack(vecs)
    meta = [{"path": p, "stamp": st} for p, st in zip(paths, kept_stamps)]
    np.savez(cache_file, vecs=arr, meta=json.dumps(meta))
    return paths, arr, computed


def search(query: Path | str, folder: Path | str, *, top: int = 10, reindex: bool = False) -> tuple[list[Match], int]:
    q = Path(query)
    qv = fingerprint(q)
    paths, vecs, computed = build_index(folder, reindex=reindex)
    allv = np.vstack([vecs, qv])
    mu, sd = allv.mean(0), allv.std(0) + 1e-9
    z, zq = (vecs - mu) / sd, (qv - mu) / sd
    sims = (z @ zq) / (np.linalg.norm(z, axis=1) * np.linalg.norm(zq) + 1e-12)
    order = np.argsort(-sims)
    out = [Match(paths[i], float(sims[i])) for i in order if Path(paths[i]).resolve() != q.resolve()][:top]
    return out, computed
