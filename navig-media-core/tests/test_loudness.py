"""The shared loudness meter: same readings the per-plugin copies produced, always bounded."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from navig_media_core.media import loudness as L

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")


@pytest.fixture(scope="module")
def tone(tmp_path_factory) -> Path:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not on PATH")
    out = tmp_path_factory.mktemp("tone") / "tone.wav"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono:d=1",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=4",
         "-filter_complex", "[0][1]concat=n=2:v=0:a=1[a]", "-map", "[a]", str(out)],
        check=True, capture_output=True, timeout=60,
    )
    return out


@needs_ffmpeg
def test_integrated_lufs_equals_the_old_ebur128_parse(tone: Path) -> None:
    """navig-video's mix/parity parsed the Summary block exactly like this."""
    r = subprocess.run(["ffmpeg", "-v", "info", "-i", str(tone), "-af", "ebur128", "-f", "null", "-"],
                       capture_output=True, text=True, timeout=120)
    m = re.search(r"I:\s*(-?\d+(?:\.\d+)?)\s*LUFS", r.stderr.split("Summary:")[-1])
    assert m is not None
    assert L.integrated_lufs(tone) == float(m.group(1))


@needs_ffmpeg
def test_windowed_measure_equals_the_old_episode_parse(tone: Path) -> None:
    """navig-pipeline's episode.loudness read input_i of a -ss/-t window like this."""
    r = subprocess.run(["ffmpeg", "-ss", "1.5", "-t", "2", "-i", str(tone),
                        "-af", "loudnorm=print_format=json", "-f", "null", "-"],
                       capture_output=True, encoding="utf-8", errors="replace", timeout=120)
    m = re.search(r'"input_i"\s*:\s*"(-?[\d.]+)"', r.stderr)
    assert m is not None
    assert L.measure(tone, start=1.5, duration=2).lufs == float(m.group(1))


def test_integrated_lufs_is_none_when_ffmpeg_hangs(monkeypatch) -> None:
    def hang(*_a, **k):
        assert k.get("timeout"), "the meter must always pass a timeout"
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=k["timeout"])

    monkeypatch.setattr(L.subprocess, "run", hang)
    assert L.integrated_lufs("x.wav", timeout=1) is None


def test_integrated_lufs_ignores_per_frame_lines_before_the_summary(monkeypatch) -> None:
    stderr = "t: 0.1 M: -70.0 S: -70.0 I: -70.0 LUFS\n[Parsed_ebur128_0] Summary:\n  I: -18.3 LUFS\n"
    monkeypatch.setattr(L.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, "", stderr))
    assert L.integrated_lufs("x.wav") == -18.3


def test_measure_puts_the_window_before_the_input(monkeypatch) -> None:
    seen: list[list[str]] = []
    payload = '{"input_i" : "-20.0", "input_lra" : "3.0", "input_tp" : "-2.0", "input_thresh" : "-30.0"}'

    def fake_run(argv, **_k):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", payload.encode())

    monkeypatch.setattr(L.shutil, "which", lambda _n: "ffmpeg")
    monkeypatch.setattr(L.subprocess, "run", fake_run)
    got = L.measure("x.wav", start=2.0, duration=3.0)
    argv = seen[0]
    assert argv[argv.index("-ss") + 1] == "2.0" and argv[argv.index("-t") + 1] == "3.0"
    assert argv.index("-ss") < argv.index("-i")
    assert (got.lufs, got.lra, got.true_peak, got.threshold) == (-20.0, 3.0, -2.0, -30.0)
