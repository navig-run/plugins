"""``navig video parity`` — prove a render matches the one it claims to replace.

  navig video parity orig.mp4 port.mp4
  navig video parity orig.mp4 port.mp4 --codec-floor --json
  navig video parity still-a.png still-b.png

Images must be byte-identical (with the pixel MAE reported when they are not).
Videos compare container (duration/frames/resolution), sampled decoded pixels
against the encoder's own noise floor when ``--codec-floor`` is given, decoded
audio (RMS) and integrated loudness. Exit 0 = parity, 1 = outside tolerance,
2 = usage (a file missing, mixed kinds, unsupported type).

Mounted on ``video_app`` by import side-effect from ``video.py``.
"""

from __future__ import annotations

import json as _json
from pathlib import Path
from typing import Optional

import typer

from navig_sdk import console as ch
from navig_video._typed_path import resolve_user_path
from navig_video.commands.video import video_app

CMD = "navig video parity"


@video_app.command("parity")
def video_parity(
    a: Path = typer.Argument(..., help="The reference render (the original)."),
    b: Path = typer.Argument(..., help="The candidate render (the port)."),
    frames: int = typer.Option(9, "--frames", min=1, max=120, help="Sampled frames across the video."),
    codec_floor: bool = typer.Option(False, "--codec-floor", help="Re-encode A once and hold B to that noise budget (tighter pixel test, ~1x render time)."),
    mae: Optional[float] = typer.Option(None, "--mae", help="Override the pixel-MAE tolerance (per 255)."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable report."),
) -> None:
    """⚖ Do two renders match? Container, pixels, audio — the porting contract."""
    from navig_video.parity import ParityError, compare

    a = resolve_user_path(a)
    b = resolve_user_path(b)
    try:
        report = compare(a, b, frames=frames, codec_floor=codec_floor, mae_tol=mae)
    except ParityError as exc:
        ch.error("Cannot compare", str(exc))
        raise typer.Exit(2) from exc

    if as_json:
        ch.raw_print(_json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
        raise typer.Exit(0 if report.ok else 1)

    for c in report.checks:
        glyph = "✓" if c["ok"] else "✗"
        ch.raw_print(f" {glyph} {c['name']}: {c['value']}  (limit {c['limit']})")
    interesting = ("mae_mean", "mae_worst", "codec_floor_worst", "audio_rms",
                   "loudness_a", "loudness_b", "frames_sampled")
    shown = {k: report.metrics[k] for k in interesting if k in report.metrics}
    if shown:
        ch.dim("  " + ", ".join(f"{k}={v}" for k, v in shown.items()))
    if report.ok:
        ch.success("Parity holds", f"{Path(report.a).name} ≡ {Path(report.b).name}")
    else:
        failed = ", ".join(c["name"] for c in report.checks if not c["ok"])
        ch.error("Parity FAILED", failed)
        raise typer.Exit(1)
