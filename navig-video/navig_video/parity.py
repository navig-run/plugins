"""Parity between two renders — the protocol a port must pass before it ships.

A port is not done when it "looks right": it is done when the ORIGINAL and the
PORT produce the same thing measurable three layers deep — container, decoded
pixels, decoded audio. This is the protocol that verified the kinetic port
(2026-10-14: stills byte-identical, duration/frames exact, audio RMS 0.000000,
decoded video within the x264 codec floor), turned into one command so the next
renderer does not rebuild it by hand.

The codec-floor step exists because two lossy encodes of the SAME frames are
not bit-identical: x264's threading decides differently run to run. Re-encoding
A once with the channel's encode settings measures that noise floor; B is held
to it (x1.5, never below 1.0/255) instead of to an arbitrary constant.

Images skip the codec question entirely — a still must be byte-identical, and if
it is not, the MAE says how far apart the pixels are.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["ParityError", "ParityReport", "compare"]

#: The channel's encode settings — the floor must be measured with the same knobs
#: the render used, or it measures a different encoder.
ENCODE_ARGS = [
    "-c:v", "libx264", "-preset", "medium", "-crf", "19",
    "-maxrate", "16M", "-bufsize", "32M", "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart",
]

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".avi"}


class ParityError(RuntimeError):
    """A comparison could not run; the message says what to fix."""


@dataclass
class ParityReport:
    kind: str                      # "image" | "video"
    a: str
    b: str
    checks: list[dict] = field(default_factory=list)   # {name, ok, value, limit}
    metrics: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(c["ok"] for c in self.checks)

    def add(self, name: str, ok: bool, value, limit) -> None:
        self.checks.append({"name": name, "ok": bool(ok), "value": value, "limit": limit})

    def to_dict(self) -> dict:
        return {"ok": self.ok, "kind": self.kind, "a": self.a, "b": self.b,
                "checks": self.checks, "metrics": self.metrics}


def _run(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def _ffprobe(path: Path) -> dict:
    r = _run(["ffprobe", "-v", "error", "-select_streams", "v:0",
              "-count_frames", "-show_entries",
              "stream=width,height,nb_read_frames,r_frame_rate",
              "-show_entries", "format=duration", "-of", "json", str(path)])
    if r.returncode != 0:
        raise ParityError(f"ffprobe failed on {path.name}: {r.stderr.strip()[:200]}")
    try:
        d = json.loads(r.stdout)
        s = d["streams"][0]
        n, den = s["r_frame_rate"].split("/")
        return {
            "duration": float(d["format"]["duration"]),
            "frames": int(s["nb_read_frames"]),
            "width": int(s["width"]),
            "height": int(s["height"]),
            "fps": float(n) / float(den),
        }
    except (KeyError, ValueError, IndexError) as exc:
        raise ParityError(f"cannot read {path.name}: {exc}") from exc


def _decode_rgb(path: Path, want: set[int]) -> dict[int, object]:
    """Frame index → RGB array for the sampled indices (one decode pass)."""
    import numpy as np

    # resolution comes from the file itself so any size compares
    info = _ffprobe(path)
    w, h = info["width"], info["height"]
    fsz = w * h * 3
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)   # early close = broken-pipe noise
    out: dict[int, object] = {}
    i = 0
    assert proc.stdout is not None
    while True:
        buf = proc.stdout.read(fsz)
        if len(buf) < fsz:
            break
        if i in want:
            out[i] = np.frombuffer(buf, np.uint8).reshape(h, w, 3).astype(np.float32)
        i += 1
        if i > max(want):
            break
    proc.stdout.close()
    proc.wait()
    return out


def _pcm_raw(path: Path) -> bytes:
    """Raw PCM bytes — text decoding would corrupt binary."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le",
                        "-ac", "1", "-ar", "48000", "-"], capture_output=True)
    if r.returncode != 0:
        raise ParityError(f"cannot decode audio of {path.name}")
    return r.stdout


def _loudness(path: Path) -> float | None:
    from navig_media_core.media.loudness import integrated_lufs  # noqa: PLC0415

    return integrated_lufs(path)


def _reencode(src: Path, dst: Path) -> Path:
    r = _run(["ffmpeg", "-v", "error", "-y", "-i", str(src), *ENCODE_ARGS, str(dst)])
    if r.returncode != 0:
        raise ParityError(f"codec-floor re-encode failed: {r.stderr.strip()[:200]}")
    return dst


def _sample_indices(frames: int, n: int) -> set[int]:
    if frames <= 0 or n <= 0:
        return set()
    if frames <= n:
        return set(range(frames))
    return {round(i * (frames - 1) / (n - 1)) for i in range(n)}


def _mae(a, b) -> float:
    import numpy as np

    return float(np.mean(np.abs(a - b)))


def compare_images(a: Path, b: Path, mae_tol: float = 1.0) -> ParityReport:
    """A still must be byte-identical; if not, how far apart are the pixels."""
    rep = ParityReport(kind="image", a=str(a), b=str(b))
    ba, bb = a.read_bytes(), b.read_bytes()
    rep.metrics["bytes_a"], rep.metrics["bytes_b"] = len(ba), len(bb)
    if ba == bb:
        rep.add("byte-identical", True, len(ba), "equal")
        return rep
    # Bytes differ — still report HOW FAR the pixels are (diagnostic; a render
    # must reproduce its still byte-for-byte, so the verdict stays a fail).
    import numpy as np
    from PIL import Image

    try:
        ia = np.asarray(Image.open(a).convert("RGB"), dtype=np.float32)
        ib = np.asarray(Image.open(b).convert("RGB"), dtype=np.float32)
        if ia.shape == ib.shape:
            rep.metrics["mae"] = round(_mae(ia, ib), 4)
            rep.metrics["mae_tol"] = mae_tol
        else:
            rep.metrics["size"] = f"{ia.shape} vs {ib.shape}"
    except OSError as exc:
        rep.metrics["pixel_error"] = str(exc)
    rep.add("byte-identical", False, "differ", "equal")
    return rep


def compare_videos(
    a: Path,
    b: Path,
    frames: int = 9,
    codec_floor: bool = False,
    mae_tol: float | None = None,
    loudness_tol: float = 0.5,
    audio_rms_tol: float = 0.01,
    duration_tol: float = 0.1,
) -> ParityReport:
    """Container → pixels → audio, with the codec's own noise as the pixel budget."""
    import numpy as np

    rep = ParityReport(kind="video", a=str(a), b=str(b))
    ia, ib = _ffprobe(a), _ffprobe(b)
    rep.metrics.update({f"{k}_a": v for k, v in ia.items()})
    rep.metrics.update({f"{k}_b": v for k, v in ib.items()})

    rep.add("duration", abs(ia["duration"] - ib["duration"]) <= duration_tol,
            round(abs(ia["duration"] - ib["duration"]), 4), f"<= {duration_tol}s")
    rep.add("frame count", ia["frames"] == ib["frames"], ib["frames"], ia["frames"])
    rep.add("resolution", (ia["width"], ia["height"]) == (ib["width"], ib["height"]),
            f"{ib['width']}x{ib['height']}", f"{ia['width']}x{ia['height']}")

    # ── pixels ──
    want = _sample_indices(min(ia["frames"], ib["frames"]), frames)
    fa, fb = _decode_rgb(a, want), _decode_rgb(b, want)
    common = sorted(set(fa) & set(fb))
    if not common:
        rep.add("sampled frames", False, 0, f">= 1 of {frames}")
        return rep
    maes = [_mae(fa[i], fb[i]) for i in common]
    worst = max(maes)
    rep.metrics["mae_mean"] = round(sum(maes) / len(maes), 4)
    rep.metrics["mae_worst"] = round(worst, 4)
    rep.metrics["frames_sampled"] = len(common)

    limit = mae_tol
    if codec_floor:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="parity-floor-") as td:
            floor_path = Path(td) / "floor.mp4"
            _reencode(a, floor_path)
            ff = _decode_rgb(floor_path, set(common))
            floor_maes = [_mae(fa[i], ff[i]) for i in common if i in ff]
            floor_worst = max(floor_maes) if floor_maes else 0.0
        rep.metrics["codec_floor_worst"] = round(floor_worst, 4)
        if mae_tol is None:
            limit = max(floor_worst * 1.5, 1.0)
    elif limit is None:
        limit = 8.0  # two lossy encodes of the same frames: loose default, no floor measured
    rep.add("pixel MAE (worst)", worst <= limit, round(worst, 4), f"<= {limit}/255")

    # ── audio ──
    pa = np.frombuffer(_pcm_raw(a), np.float32)
    pb = np.frombuffer(_pcm_raw(b), np.float32)
    n = min(len(pa), len(pb))
    rep.metrics["pcm_samples"] = int(n)
    if n == 0:
        rep.add("decoded audio", False, 0, "> 0 samples")
    else:
        rms = float(np.sqrt(np.mean((pa[:n] - pb[:n]) ** 2)))
        rep.metrics["audio_rms"] = round(rms, 6)
        rep.add("audio RMS", rms <= audio_rms_tol, round(rms, 6), f"<= {audio_rms_tol}")
    la, lb = _loudness(a), _loudness(b)
    if la is not None and lb is not None:
        delta = abs(la - lb)
        rep.metrics["loudness_a"], rep.metrics["loudness_b"] = la, lb
        rep.add("loudness", delta <= loudness_tol, round(delta, 3), f"<= {loudness_tol} LUFS")
    return rep


def compare(
    a: Path,
    b: Path,
    frames: int = 9,
    codec_floor: bool = False,
    mae_tol: float | None = None,
) -> ParityReport:
    """Compare two renders — images byte-exact, videos through the full protocol."""
    for p in (a, b):
        if not p.is_file():
            raise ParityError(f"not found: {p}")
    suffix = a.suffix.lower()
    if suffix != b.suffix.lower():
        raise ParityError(f"mixed kinds: {a.suffix} vs {b.suffix}")
    if suffix in IMAGE_SUFFIXES:
        return compare_images(a, b, mae_tol=mae_tol if mae_tol is not None else 1.0)
    if suffix in VIDEO_SUFFIXES:
        return compare_videos(a, b, frames=frames, codec_floor=codec_floor,
                              mae_tol=mae_tol)
    raise ParityError(f"unsupported file type: {suffix or '(none)'}")
