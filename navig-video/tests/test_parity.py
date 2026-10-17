"""``navig video parity`` — the porting contract, executable.

Every fixture is made with ffmpeg on the spot (no checked-in media): a copy
compares equal, a re-encode stays inside the codec floor it measures, a trimmed
clip fails on container facts. Image parity is byte-equality — the bar the
kinetic stills met 3/3.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from navig_video.cli import _standalone_app
from navig_video.parity import ParityError, compare

runner = CliRunner()
FFMPEG = shutil.which("ffmpeg")

pytestmark = pytest.mark.skipif(not FFMPEG, reason="ffmpeg not on PATH")


def _clip(path: Path, seconds: str = "1") -> Path:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"testsrc2=duration={seconds}:size=320x240:rate=30",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-shortest", "-pix_fmt", "yuv420p", str(path)],
        check=True)
    return path


def _reencode(src: Path, dst: Path) -> Path:
    from navig_video.parity import ENCODE_ARGS

    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), *ENCODE_ARGS, str(dst)],
                   check=True)
    return dst


# ── videos ───────────────────────────────────────────────────────────────────
def test_a_copy_passes_every_check(tmp_path: Path):
    a = _clip(tmp_path / "a.mp4")
    b = tmp_path / "b.mp4"
    shutil.copyfile(a, b)
    rep = compare(a, b, codec_floor=True)
    assert rep.ok, rep.to_dict()
    assert all(c["ok"] for c in rep.checks)
    assert rep.metrics["audio_rms"] == 0.0


def test_a_reencode_stays_inside_its_own_codec_floor(tmp_path: Path):
    """Two encodes of one source: the floor measures the noise, B passes under it."""
    a = _clip(tmp_path / "a.mp4")
    b = _reencode(a, tmp_path / "b.mp4")
    rep = compare(a, b, codec_floor=True)
    assert rep.ok, rep.to_dict()
    floor = rep.metrics["codec_floor_worst"]
    assert floor >= rep.metrics["mae_worst"] - 0.001 or rep.metrics["mae_worst"] <= floor * 1.5 + 0.5


def test_a_trimmed_clip_fails_on_container_facts(tmp_path: Path):
    a = _clip(tmp_path / "a.mp4", seconds="1")
    b = tmp_path / "b.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(a), "-t", "0.5", str(b)],
                   check=True)
    rep = compare(a, b)
    assert not rep.ok
    failed = {c["name"] for c in rep.checks if not c["ok"]}
    assert "frame count" in failed or "duration" in failed, failed


# ── images ────────────────────────────────────────────────────────────────────
def test_identical_stills_pass(tmp_path: Path):
    pytest.importorskip("PIL")
    from PIL import Image

    Image.new("RGB", (64, 64), (10, 20, 30)).save(tmp_path / "a.png")
    shutil.copyfile(tmp_path / "a.png", tmp_path / "b.png")
    rep = compare(tmp_path / "a.png", tmp_path / "b.png")
    assert rep.ok, rep.to_dict()


def test_different_stills_fail_and_report_mae(tmp_path: Path):
    pytest.importorskip("PIL")
    from PIL import Image

    Image.new("RGB", (64, 64), (10, 20, 30)).save(tmp_path / "a.png")
    Image.new("RGB", (64, 64), (200, 30, 30)).save(tmp_path / "b.png")
    rep = compare(tmp_path / "a.png", tmp_path / "b.png")
    assert not rep.ok
    assert rep.metrics.get("mae", 0) > 1.0


# ── error paths + CLI ────────────────────────────────────────────────────────
def test_missing_file_is_a_parity_error(tmp_path: Path):
    with pytest.raises(ParityError, match="not found"):
        compare(tmp_path / "nope.mp4", tmp_path / "also-nope.mp4")


def test_cli_parity_pass_exits_0(tmp_path: Path):
    a = _clip(tmp_path / "a.mp4")
    b = tmp_path / "b.mp4"
    shutil.copyfile(a, b)
    res = runner.invoke(_standalone_app(), ["parity", str(a), str(b)])
    assert res.exit_code == 0, res.output
    assert "Parity holds" in res.output


def test_cli_parity_fail_exits_1(tmp_path: Path):
    a = _clip(tmp_path / "a.mp4", seconds="1")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(a), "-t", "0.5",
                    str(tmp_path / "b.mp4")], check=True)
    res = runner.invoke(_standalone_app(), ["parity", str(a), str(tmp_path / "b.mp4")])
    assert res.exit_code == 1, res.output
    assert "FAILED" in res.output


def test_cli_parity_missing_file_exits_2(tmp_path: Path):
    res = runner.invoke(_standalone_app(), ["parity",
                                            str(tmp_path / "x.mp4"),
                                            str(tmp_path / "y.mp4")])
    assert res.exit_code == 2


def test_cli_parity_json(tmp_path: Path):
    a = _clip(tmp_path / "a.mp4")
    b = tmp_path / "b.mp4"
    shutil.copyfile(a, b)
    res = runner.invoke(_standalone_app(), ["parity", str(a), str(b), "--json"])
    assert res.exit_code == 0, res.output
    payload = __import__("json").loads(res.output)
    assert payload["ok"] is True
    assert payload["kind"] == "video"
