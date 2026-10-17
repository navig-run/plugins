"""``navig video cut`` — the EDL renderer (plan 21's editable timeline).

The contract: an EDL in, its outputs on disk (both streams, durations within
tolerance of the targets), sources never touched. Usage problems (unparseable
EDL, unknown source, out-of-bounds segment) exit 2; ffmpeg failures exit 1;
existing outputs are kept unless ``--force``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from navig_video.cli import _standalone_app

runner = CliRunner()

ffmpeg_available = shutil.which("ffmpeg") is not None
requires_ffmpeg = pytest.mark.skipif(not ffmpeg_available, reason="ffmpeg not on PATH")


def _av(path: Path, seconds: float) -> Path:
    """A short clip WITH sound — the material EDLs are cut from."""
    subprocess.run(
        [shutil.which("ffmpeg"), "-nostdin", "-y", "-f", "lavfi",
         "-i", f"testsrc=size=640x360:rate=30:duration={seconds:g}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds:g}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
         "-shortest", str(path)],
        capture_output=True, check=True,
    )
    return path


def _streams(path: Path) -> set[str]:
    out = subprocess.run(
        [shutil.which("ffprobe"), "-v", "error", "-show_entries", "stream=codec_type",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return {line.strip() for line in out.stdout.splitlines() if line.strip()}


def _duration(path: Path) -> float:
    out = subprocess.run(
        [shutil.which("ffprobe"), "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """A mini postpro dir: one source, one SRT, an EDL with two outputs."""
    _av(tmp_path / "take.mp4", 4.0)
    (tmp_path / "captions.srt").write_text(
        "1\n00:00:00,100 --> 00:00:01,500\nHello there.\n\n"
        "2\n00:00:01,600 --> 00:00:03,000\nSecond line.\n",
        encoding="utf-8",
    )
    edl = {
        "version": 1,
        "sources": [{"id": "take", "path": "take.mp4", "duration": 4.0,
                     "width": 640, "height": 360, "fps": 30}],
        "outputs": [
            {"id": "trim", "kind": "trim", "target_s": 2.0, "aspect": "9:16",
             "segments": [{"src": "take", "t0": 0.5, "t1": 2.5}],
             "audio": {"i": -14.0, "tp": -1.0}, "file": "out/trim.mp4"},
            {"id": "assembly", "kind": "assembly", "target_s": 3.0, "aspect": "9:16",
             "segments": [{"src": "take", "t0": 0.0, "t1": 1.5},
                          {"src": "take", "t0": 2.5, "t1": 4.0}],
             "audio": {"i": -14.0, "tp": -1.0},
             "captions": {"srt": "captions.srt", "burn": True},
             "file": "out/captioned.mp4"},
        ],
    }
    (tmp_path / "edl.json").write_text(json.dumps(edl), encoding="utf-8")
    return tmp_path


# ── happy path ────────────────────────────────────────────────────────────────
@requires_ffmpeg
def test_cut_renders_every_output_with_both_streams(project: Path) -> None:
    res = runner.invoke(_standalone_app(), ["cut", str(project / "edl.json"), "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert {o["id"] for o in data["outputs"]} == {"trim", "assembly"}
    trim = project / "out" / "trim.mp4"
    cap = project / "out" / "captioned.mp4"
    assert trim.is_file() and cap.is_file()
    assert _streams(trim) == {"video", "audio"}
    assert _streams(cap) == {"video", "audio"}
    assert abs(_duration(trim) - 2.0) <= 0.2
    # assembly = two 1.5 s takes, hard-cut
    assert abs(_duration(cap) - 3.0) <= 0.25
    # sources are read-only: the take still has its original length
    assert abs(_duration(project / "take.mp4") - 4.0) <= 0.15


@requires_ffmpeg
def test_cut_skips_existing_outputs_and_force_re_renders(project: Path) -> None:
    first = runner.invoke(_standalone_app(), ["cut", str(project / "edl.json"), "--json"])
    assert first.exit_code == 0, first.output
    trim = project / "out" / "trim.mp4"
    stamp = trim.stat().st_mtime_ns

    again = runner.invoke(_standalone_app(), ["cut", str(project / "edl.json"), "--json"])
    assert again.exit_code == 0, again.output
    data = json.loads(again.output)
    states = {o["id"]: o["cached"] for o in data["outputs"]}
    assert states == {"trim": True, "assembly": True}, "existing outputs are kept"
    assert trim.stat().st_mtime_ns == stamp

    forced = runner.invoke(
        _standalone_app(), ["cut", str(project / "edl.json"), "--force", "--json"])
    assert forced.exit_code == 0, forced.output
    data = json.loads(forced.output)
    assert all(o["cached"] is False for o in data["outputs"])


# ── usage errors exit 2 ───────────────────────────────────────────────────────
def test_an_unparseable_edl_exits_2(project: Path) -> None:
    (project / "bad.json").write_text("{not json", encoding="utf-8")
    res = runner.invoke(_standalone_app(), ["cut", str(project / "bad.json")])
    assert res.exit_code == 2
    assert "EDL" in res.output


def test_an_unknown_source_exits_2(project: Path) -> None:
    edl = json.loads((project / "edl.json").read_text(encoding="utf-8"))
    edl["outputs"][0]["segments"][0]["src"] = "ghost"
    (project / "edl.json").write_text(json.dumps(edl), encoding="utf-8")
    res = runner.invoke(_standalone_app(), ["cut", str(project / "edl.json")])
    assert res.exit_code == 2
    assert "ghost" in res.output


def test_an_out_of_bounds_segment_exits_2(project: Path) -> None:
    edl = json.loads((project / "edl.json").read_text(encoding="utf-8"))
    edl["outputs"][0]["segments"][0]["t1"] = 99.0
    (project / "edl.json").write_text(json.dumps(edl), encoding="utf-8")
    res = runner.invoke(_standalone_app(), ["cut", str(project / "edl.json")])
    assert res.exit_code == 2
    assert "99" in res.output


def test_a_missing_caption_file_exits_2(project: Path) -> None:
    (project / "captions.srt").unlink()
    res = runner.invoke(_standalone_app(), ["cut", str(project / "edl.json")])
    assert res.exit_code == 2
    assert "caption" in res.output.lower()
