"""Kinetic: the timeline's arithmetic, a micro render, and the CLI contract.

The timeline tests stub the three external seams (voice synthesis, bed store,
grid) and pin the arithmetic that decides every cut: lead-in, the on-beat
clamp, red masks, display-vs-spoken regrouping. The render test is a real
ffmpeg round-trip on a two-beat micro episode — skipped where ffmpeg or the
channel's fonts are absent.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from navig_video.cli import _standalone_app
from navig_video.kinetic.ctx import bind
from navig_video.kinetic.timeline import LEAD_IN, OPEN_HOLD, plan, red_mask, regroup

runner = CliRunner()
FFMPEG = shutil.which("ffmpeg")
BEBA = Path.home() / "AppData/Local/Microsoft/Windows/Fonts" / "BebasNeue Bold.ttf"


# ── fixtures ────────────────────────────────────────────────────────────────
EPISODES_PY = '''\
EPISODES = {
    "micro": {
        "end": {"en": "Find five."},
        "tx": "001",
        "coords": "0°00′N 0°00′E",
        "motion": "rise",
        "beats": [
            {"plate": "p1", "pause": 0.0, "say": {"en": "One *red* two."}},
            {"plate": "p1", "pause": 3.0, "say": {"en": "3 is a crew."}},
        ],
    },
}
VOICES = {"en": "brian"}
VOICES2 = {}
'''


@pytest.fixture()
def ws(tmp_path: Path) -> Path:
    ws = tmp_path
    (ws / "sound" / "kit").mkdir(parents=True)
    (ws / "vo" / "kinetic").mkdir(parents=True)
    (ws / "plates" / "gen").mkdir(parents=True)
    (ws / "out").mkdir(parents=True)
    (ws / "episodes.py").write_text(EPISODES_PY, encoding="utf-8", newline="\n")
    (ws / "build.py").write_text(
        "def flat(raw):\n    return dict(raw)\n", encoding="utf-8", newline="\n")
    (ws / "live.py").write_text(
        "def fetch():\n    return {}\n", encoding="utf-8", newline="\n")
    # a seeded bed store pointing at a real (empty) file — assign() only stats it
    bed = ws / "bed.mp3"
    bed.write_bytes(b"stub")
    (ws / "sound" / "bed-assign.json").write_text(
        json.dumps({"micro": {"path": str(bed), "at": 0.0, "mood": "dark"}}),
        encoding="utf-8")
    # a seeded grid: exactly 10 half-second beats
    import hashlib

    key = hashlib.sha1(f"{bed}|0.0|60.0".encode()).hexdigest()[:12]
    (ws / "sound" / "grids").mkdir(parents=True)
    (ws / "sound" / "grids" / f"{key}.json").write_text(
        json.dumps([round(0.5 * i, 3) for i in range(60)]), encoding="utf-8")
    return ws


def _stub_voice(monkeypatch: pytest.MonkeyPatch, dur: float = 0.4, voice_path: Path | None = None):
    """Replace synthesis at its source — plan() imports it at call time."""
    from navig_audio.voice import line as line_mod

    class R:
        def __init__(self, text):
            words = text.split()
            step = dur / max(len(words), 1)
            self.words = [{"w": w, "s": i * step, "e": (i + 1) * step}
                          for i, w in enumerate(words)]
            self.path = voice_path or Path("stub.mp3")

    monkeypatch.setattr(line_mod, "synth_timed",
                        lambda text, voice, lang, cache, **k: R(text))
    monkeypatch.setattr(line_mod, "duration", lambda p: dur)


# ── timeline arithmetic ─────────────────────────────────────────────────────
def test_plan_arithmetic(ws: Path, monkeypatch: pytest.MonkeyPatch):
    _stub_voice(monkeypatch, dur=0.4)
    bind(ws)
    tl = plan("micro", "en", {})
    assert len(tl.beats) == 2
    b0, b1 = tl.beats
    # beat 0: starts after the title open (lead-in + 3 s hold), ends on the
    # music (pause 0 → on-beat clamp)
    assert b0.t0 == LEAD_IN + OPEN_HOLD
    assert b0.t1 in (3.5, 4.0), f"cut should land on a grid beat, got {b0.t1}"
    # beat 1: pause 3.0 would blow past the 0.45 s clamp → written pause survives
    assert b1.t0 == b0.t1
    assert b1.t1 > b1.t0
    # words timed absolutely, red mask applied to display tokens
    assert [w["w"] for w in b0.words] == ["One", "red", "two."]
    assert [w["red"] for w in b0.words] == [False, True, False]
    assert b0.sfx is None and b1.sfx == "hit_low"   # first hard cut is silent
    assert tl.motion == "rise"
    assert tl.total == pytest.approx(tl.end_t0 + 2.2)
    # segs: same plate → one continuous shot
    assert b0.seg == b1.seg == 0


def test_red_mask_edges():
    assert red_mask("plain words") == [False, False]
    assert red_mask("*one* two") == [True, False]
    assert red_mask("a *mid start* end") == [False, True, True, False]
    assert red_mask("«*quoted*».") == [True]  # one token: opens and closes


def test_regroup_folds_spoken_onto_display():
    disp = ["266", "zones"]
    groups = [["двести", "шестьдесят", "шесть"], ["зоны"]]
    spoken = [{"w": "двести", "s": 0.0, "e": 0.4},
              {"w": "шестьдесят", "s": 0.4, "e": 0.8},
              {"w": "шесть", "s": 0.8, "e": 1.0},
              {"w": "зоны", "s": 1.2, "e": 1.4}]
    out = regroup(disp, groups, spoken)
    assert out == [{"w": "266", "s": 0.0, "e": 1.0},
                   {"w": "zones", "s": 1.2, "e": 1.4}]
    # a count mismatch falls back to showing the spoken words as they are
    assert regroup(disp, groups, spoken[:2]) == spoken[:2]


# ── micro render (real ffmpeg) ──────────────────────────────────────────────
def _kit_and_plates(ws: Path):
    kit = ws / "sound" / "kit"
    for name in ("hit_low", "radio", "swell", "boom", "reveal", "glitch"):
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
             "-i", "anullsrc=r=44100:cl=mono", "-t", "0.3",
             str(kit / f"{name}.wav")], check=True)
    # a bed with actual audio (assign stats it; grid is pre-seeded)
    bed = ws / "bed.mp3"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", "sine=frequency=80:duration=40", str(bed)], check=True)
    # a real plate + a stub voice mp3 of the right shape
    from PIL import Image

    Image.new("RGB", (1200, 2200), (18, 20, 26)).save(ws / "plates" / "gen" / "p1.png")
    voice = ws / "vo" / "kinetic" / "stub.mp3"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", "anullsrc=r=44100:cl=mono", "-t", "0.4", str(voice)], check=True)


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not on PATH")
@pytest.mark.skipif(not BEBA.exists(), reason="Bebas Neue Bold not installed")
def test_micro_render_roundtrip(ws: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _kit_and_plates(ws)
    _stub_voice(monkeypatch, dur=0.4, voice_path=ws / "vo" / "kinetic" / "stub.mp3")
    bind(ws, out=tmp_path / "out")
    from navig_video.kinetic.render import render

    mp4 = render("micro", "en", {})
    assert mp4.exists()
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-show_entries", "format=duration",
         "-of", "json", str(mp4)], capture_output=True, text=True)
    info = json.loads(r.stdout)
    dur = float(info["format"]["duration"])
    assert dur >= 1.0, "render shorter than its own timeline"
    assert (info["streams"][0]["width"], info["streams"][0]["height"]) == (1080, 1920)
    # the mix sidecar exists with a measured loudness
    audio_note = mp4.parent / "_micro-audio.json"
    assert audio_note.exists()
    note = json.loads(audio_note.read_text(encoding="utf-8"))
    assert "integrated_lufs" in note


# ── CLI contract ────────────────────────────────────────────────────────────
def test_cli_unknown_episode_exits_2(ws: Path):
    res = runner.invoke(_standalone_app(),
                        ["kinetic", "plan", "nope", "--workspace", str(ws)])
    assert res.exit_code == 2, res.output


def test_cli_missing_workspace_exits_2(tmp_path: Path):
    res = runner.invoke(_standalone_app(),
                        ["kinetic", "plan", "x", "--workspace", str(tmp_path / "gone")])
    assert res.exit_code == 2
    assert "not found" in res.output.lower() or "Workspace" in res.output


def test_cli_plan_json(ws: Path, monkeypatch: pytest.MonkeyPatch):
    _stub_voice(monkeypatch, dur=0.4)
    res = runner.invoke(
        _standalone_app(),
        ["kinetic", "plan", "micro", "--workspace", str(ws), "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["episode"] == "micro"
    assert len(data["beats"]) == 2
    w = data["beats"][0]["words"][1]
    step = 0.4 / 3
    assert w["w"] == "red" and w["red"] is True
    assert w["s"] == pytest.approx(LEAD_IN + OPEN_HOLD + step, abs=0.01)
    assert w["e"] == pytest.approx(LEAD_IN + OPEN_HOLD + 2 * step, abs=0.01)


# ── fonts ────────────────────────────────────────────────────────────────────
def test_missing_fonts_names_the_face(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from navig_video.kinetic import pictures

    monkeypatch.setattr(pictures, "F_SLAM", str(tmp_path / "nope.ttf"))
    # the name reported is the file that failed to load (in production that is
    # "BebasNeue Bold.ttf"); Path().name keeps it a name on mixed separators
    assert "nope.ttf" in pictures.missing_fonts()


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not on PATH")
def test_cli_render_refuses_before_any_work_when_a_font_is_missing(
    ws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from navig_video.kinetic import pictures

    # voice stub as a safety net: if the precheck ever regresses, the render
    # must fail on missing plates instead of reaching a real synth call
    _stub_voice(monkeypatch, dur=0.4, voice_path=ws / "vo" / "kinetic" / "stub.mp3")
    monkeypatch.setattr(pictures, "F_SLAM", str(tmp_path / "nope.ttf"))
    res = runner.invoke(
        _standalone_app(),
        ["kinetic", "render", "micro", "--workspace", str(ws), "--json"],
    )
    assert res.exit_code == 1, res.output
    assert "Font missing" in res.output
    assert "nope.ttf" in res.output
    assert not any((ws / "out").rglob("*.mp4")), "refusal must happen before any render"
    assert not any((ws / "vo" / "kinetic").glob("*.mp3")), "refusal must happen before synth"
