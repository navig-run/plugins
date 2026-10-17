"""Loudness as streaming services and mastering engineers count it — not RMS.

The media packages' ONE loudness meter (ADR-0002). Two readings, kept distinct on purpose:

* :func:`measure` — ffmpeg's ``loudnorm`` analysis pass: integrated LUFS, loudness range (LU),
  true peak (dBTP) and the gating threshold (needed to re-apply ``loudnorm``). Optionally over a
  window (``start``/``duration``). Moved here from ``navig_audio.mastering.loudness``, which is
  now an identity alias of this module.
* :func:`integrated_lufs` — the ``ebur128`` filter's Summary integrated loudness. A different
  meter implementation than ``loudnorm``; the two can differ by a few tenths of a LU, so callers
  that gate on one (video mix, parity) keep the one they were tuned against.

Both always run with a timeout: a file ffmpeg chokes on must not hang the caller.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from navig_sdk.host import decode_console_result


class LoudnessError(RuntimeError):
    """ffmpeg is missing or could not read the file."""


@dataclass(frozen=True)
class Loudness:
    path: str
    lufs: float  # integrated loudness, LUFS
    lra: float  # loudness range, LU
    true_peak: float  # dBTP
    threshold: float  # the gating threshold the meter used (needed to re-apply loudnorm)

    def as_dict(self) -> dict:
        return asdict(self)


_JSON = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.S)
_EBUR128_I = re.compile(r"I:\s*(-?\d+(?:\.\d+)?)\s*LUFS")


def measure(
    path: Path | str,
    *,
    timeout: int = 600,
    start: float | None = None,
    duration: float | None = None,
) -> Loudness:
    """Measure one file (or the ``[start, start+duration)`` window of it).

    Silence reads as -inf LUFS, which is reported, not hidden.
    """
    exe = shutil.which("ffmpeg")
    if not exe:
        raise LoudnessError("ffmpeg is not on PATH — install it (scoop install ffmpeg) and try again")
    path = Path(path)
    window: list[str] = []
    if start is not None:
        window += ["-ss", str(start)]
    if duration is not None:
        window += ["-t", str(duration)]
    try:
        proc = decode_console_result(subprocess.run(
            [exe, "-hide_banner", "-nostats", "-nostdin", *window, "-i", str(path), "-vn",
             "-af", "loudnorm=print_format=json", "-f", "null", "-"],
            capture_output=True, timeout=timeout, check=False,
        ))
    except subprocess.TimeoutExpired as exc:
        raise LoudnessError(f"ffmpeg timed out measuring {path.name}") from exc
    m = _JSON.search(proc.stderr or "")
    if proc.returncode != 0 or not m:
        tail = (proc.stderr or "").strip().splitlines()
        raise LoudnessError(f"could not measure {path.name}: {tail[-1] if tail else 'no output'}")
    data = json.loads(m.group(0))
    return Loudness(path=str(path), lufs=_num(data["input_i"]), lra=_num(data["input_lra"]),
                    true_peak=_num(data["input_tp"]), threshold=_num(data["input_thresh"]))


def integrated_lufs(path: Path | str, *, timeout: int = 600) -> float | None:
    """Integrated loudness from the ``ebur128`` filter's Summary, or None when unreadable.

    Only the Summary block counts: per-frame lines also print ``I:``, and a lead-in of
    silence would read as -70.
    """
    exe = shutil.which("ffmpeg") or "ffmpeg"
    try:
        proc = subprocess.run(
            [exe, "-v", "info", "-i", str(path), "-af", "ebur128", "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    tail = (proc.stderr or "").split("Summary:")[-1]
    m = _EBUR128_I.search(tail)
    return float(m.group(1)) if m else None


def _num(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("-inf")
