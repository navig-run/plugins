"""`from_frames` delivers what its contract promises: limited-range yuv420p.

Screenshots are JPEG — full range (yuvj4xxp). `format=yuv420p` alone kept the range, libx264
wrote the deprecated `yuvj420p`, and every stage render inherited it through stream-copy
joins (measured by `navig pipeline qa --video` on the shipped series).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from navig_media_core.media.video_edit import from_frames


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg not installed")
def test_jpeg_frames_come_out_limited_range_yuv420p(tmp_path: Path) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=270x480:rate=1",
                    "-frames:v", "2", "-q:v", "2", str(tmp_path / "f%d.jpg")], check=True, capture_output=True)
    frames = [(tmp_path / "f1.jpg", 0.5), (tmp_path / "f2.jpg", 0.5)]
    for width, height in ((1080, 1920), (None, None)):  # both filter branches
        out = from_frames(frames, tmp_path / f"out-{width}.mp4", fps=30, width=width, height=height).path
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                "stream=pix_fmt,color_range", "-of", "csv=p=0", str(out)],
                               capture_output=True, encoding="utf-8", check=True).stdout.strip()
        assert probe.split(",")[:2] == ["yuv420p", "tv"], probe
