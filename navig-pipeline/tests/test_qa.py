"""Pre-flight QA — every rule pinned to the shipped bug it now catches.

The cue fixtures are the first narrated series' REAL text (E07's stranded "files.", E06's
lone "Go."), the capture header is E02's real one, and the layout boxes are the numbers
the stage measured. A rule that never fires on the bug it was written for is vacuous, so
each bad case is paired with the fixed one passing.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from navig_pipeline.qa import PANEL_TUCK, SAFE_BOTTOM, check_layout, lint_scene, probe_video


def _scene(*cues: str, **terminal) -> dict:
    seg = {"type": "terminal", "speech": {"cues": [[c, i, i + 1] for i, c in enumerate(cues)], "words": []}}
    seg.update(terminal)
    return {"segments": [{"type": "hook", "title": "t"}, seg]}


def _rules(found) -> list[tuple[str, str]]:
    return [(f.level, f.rule) for f in found]


def test_a_stranded_sentence_tail_is_an_error() -> None:
    shipped = _scene("Then it asked to delete production", "files. Fortunately. Mine has boundaries.")
    assert ("error", "stranded-tail") in _rules(lint_scene(shipped))
    fixed = _scene("Then it asked to delete production files.", "Fortunately. Mine has boundaries.")
    assert lint_scene(fixed) == []


def test_a_lone_word_screen_is_an_error() -> None:
    assert ("error", "orphan-cue") in _rules(lint_scene(_scene("Eleven free right now.", "Go.")))
    assert lint_scene(_scene("Eleven free right now. Go.")) == []


def test_a_mid_clause_split_only_warns() -> None:
    found = lint_scene(_scene("Mine tried to restart nginx on", "production, while another agent was"))
    assert _rules(found) == [("warn", "split-sentence")]


def test_a_capture_flag_typed_on_screen_is_an_error() -> None:
    header = {1: {"command": "navig mobile osint apps com.samsung.android.app.notes --udid device-02"}}
    assert ("error", "capture-command") in _rules(lint_scene(_scene("Even the built-in notes app."), header))
    # the fix E02 shipped: a canonical `command` overrides what the header would type
    fixed = _scene("Even the built-in notes app.", command="navig mobile osint apps com.samsung.android.app.notes")
    assert lint_scene(fixed, header) == []


def test_a_command_that_wraps_three_lines_warns() -> None:
    found = lint_scene(_scene("ok then fine.", command="navig " + "x" * 80))
    assert ("warn", "long-command") in _rules(found)


def test_a_caption_running_into_its_panel_is_an_error() -> None:
    box = lambda t, b: {"top": t, "bottom": b, "left": 56, "right": 900}  # noqa: E731
    # E05 before 0.4.1: a 3-line transcript reached ~460 under a panel fixed at 440
    hit = [{"index": 1, "type": "terminal", "caption": box(180, 460), "panel": box(440, 1300)}]
    assert _rules(check_layout(hit)) == [("error", "caption-collision")]
    clear = [{"index": 1, "type": "terminal", "caption": box(180, 385), "panel": box(440, 1300)}]
    assert check_layout(clear) == []


def test_panels_may_tuck_under_the_strips_but_words_may_not() -> None:
    box = lambda b: {"top": 600, "bottom": b, "left": 40, "right": 930}  # noqa: E731
    tucked = [{"index": 1, "type": "telegram", "caption": None, "panel": box(SAFE_BOTTOM + PANEL_TUCK)}]
    assert check_layout(tucked) == []  # promo B's chat frame, by design (stage.css `- 40px`)
    too_far = [{"index": 1, "type": "telegram", "caption": None, "panel": box(SAFE_BOTTOM + PANEL_TUCK + 1)}]
    assert _rules(check_layout(too_far)) == [("warn", "safe-bottom")]
    words = [{"index": 1, "type": "terminal", "caption": box(SAFE_BOTTOM + 1), "panel": None}]
    assert _rules(check_layout(words)) == [("warn", "safe-bottom")]


needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def _clip(tmp_path: Path, pix: str, audio: bool = True, size: str = "1080x1920") -> Path:
    out = tmp_path / f"{pix}-{audio}.mp4"
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=black:s={size}:r=30:d=1"]
    if audio:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-shortest", "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-pix_fmt", pix, str(out)]
    subprocess.run(cmd, check=True, capture_output=True)
    return out


@needs_ffmpeg
def test_a_clean_render_passes_the_spec_probe(tmp_path: Path) -> None:
    assert probe_video(_clip(tmp_path, "yuv420p"), expect_secs=1.0) == []


@needs_ffmpeg
def test_full_range_warns_and_non_420_or_silent_errors(tmp_path: Path) -> None:
    # every stage render before media-core's range fix came out yuvj420p
    assert _rules(probe_video(_clip(tmp_path, "yuvj420p"))) == [("warn", "pix-range")]
    assert ("error", "pix-fmt") in _rules(probe_video(_clip(tmp_path, "yuv444p")))
    assert ("error", "no-audio") in _rules(probe_video(_clip(tmp_path, "yuv420p", audio=False)))
    assert ("error", "size") in _rules(probe_video(_clip(tmp_path, "yuv420p", size="1920x1080")))
