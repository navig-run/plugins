"""Episode builds — order, stop-at-first-failure, spend gate, cut planning.

The runner is injected, so the stage logic is tested without ffmpeg, a browser or a
voice API; `narrate` itself was verified against the studio script on a real episode
(identical cuts, bed level, gain and cues) before this module replaced it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from navig_pipeline import episode as E


@pytest.fixture()
def ep(tmp_path: Path) -> Path:
    d = tmp_path / "series" / "e07"
    d.mkdir(parents=True)
    (d / "scene-v2.json").write_text(json.dumps({"segments": [{"type": "hook"}, {"type": "outro"}]}), encoding="utf-8")
    (d / "clip-v2.md").write_text('---\naudio: _vo/track-final.mp3\nsections: [0, 3.5]\n---\n'
                                  '## Hook\n[shot: url="stage:scene-v2.json#0"]\n', encoding="utf-8")
    return d


class FakeRunner:
    """Answers each verb like the real CLI would; records the order it was called in."""

    def __init__(self, fail: str | None = None, generate: int = 0):
        self.calls: list[str] = []
        self.fail, self.generate = fail, generate

    def __call__(self, argv: list[str], cwd: Path):
        verb = argv[0] if argv[0] != "qa" else ("qa-video" if "--video" in argv else "qa")
        if argv[0] == "clip":
            verb = "plan" if "--dry-run" in argv else "render"
        self.calls.append(verb)
        if verb == self.fail:
            return 1, {"findings": [{"level": "error", "where": "seg 1", "rule": "x", "message": "boom"}]}
        if verb == "plan":
            return 0, {"generate": self.generate, "audio_s": 24.63}
        if verb == "render":
            out = cwd / argv[argv.index("-o") + 1]
            out.mkdir(exist_ok=True)
            (out / "clip.mp4").write_bytes(b"x")
            return 0, {}
        if verb == "publish":
            return 0, {"title": "T", "cover": "c.jpg", "entry": "e.md"}
        return 0, {"findings": []}


def test_stages_run_in_order_and_narrate_skips_without_a_plan(ep: Path) -> None:
    run = FakeRunner()
    stages = E.build(ep, runner=run)
    assert [s.name for s in stages] == list(E.STAGES)
    assert [s.status for s in stages] == ["skipped", "ran", "ran", "ran", "ran", "ran"]
    assert run.calls == ["qa", "plan", "render", "qa-video", "publish", "recipe"]


@pytest.mark.parametrize("fail", ["qa", "render", "qa-video", "publish"])
def test_the_first_failure_stops_everything_after_it(ep: Path, fail: str) -> None:
    run = FakeRunner(fail=fail)
    stages = {s.name: s for s in E.build(ep, runner=run)}
    failed = [n for n, s in stages.items() if s.status == "failed"]
    assert failed == [fail]
    order = list(E.STAGES)
    after = order[order.index(fail) + 1:]
    assert all(stages[n].status == "pending" for n in after)
    assert "recipe" not in run.calls


def test_paid_video_shots_are_refused_without_consent(ep: Path) -> None:
    run = FakeRunner(generate=2)
    stages = {s.name: s for s in E.build(ep, runner=run)}
    assert stages["render"].status == "failed" and "--allow-spend" in stages["render"].detail
    assert "render" not in run.calls        # the dry run refused; nothing was rendered
    assert E.build(ep, runner=FakeRunner(generate=2), allow_spend=True)[2].status == "ran"


def test_uncached_voice_is_refused_before_any_api_call(tmp_path: Path, monkeypatch) -> None:
    import navig_audio.voice.line as line

    monkeypatch.setattr(line, "synth_timed", lambda *a, **k: pytest.fail("billed without consent"))
    cfg = {"lines": [["n1", "daniel", "A line nobody voiced yet.", 0.5]], "lang": "en"}
    with pytest.raises(E.EpisodeError, match="--allow-spend"):
        E.synthesize(cfg, tmp_path / "cache", allow_spend=False)


def test_cuts_come_from_line_ends_and_starts() -> None:
    lines = [{"id": "n1", "dur": 3.0, "end": 3.0}, {"id": "n2", "dur": 4.0, "end": 8.0},
             {"id": "n3", "dur": 2.0, "end": 12.0}]
    cfg = {"segs": [{"line": "n1", "hold": 0.5}, {"until": "n3", "lead": 0.5}, {}]}
    assert E.plan_sections(cfg, lines) == [0.0, 3.5, 9.5]
    with pytest.raises(E.EpisodeError, match="backwards"):
        E.plan_sections({"segs": [{"line": "n2", "hold": 0.0}, {"line": "n1", "hold": 0.0}, {}]}, lines)


def test_an_existing_shotlist_only_has_its_cuts_moved(ep: Path) -> None:
    md = ep / "clip-v2.md"
    md.write_text(md.read_text(encoding="utf-8").replace("## Hook", "## Lock (hand-named)"), encoding="utf-8")
    E.write_clip_md(md, [0.0, 4.2], audio="_vo-fr/track-final.mp3", scene="scene-v2.json", segments=2, title="t")
    text = md.read_text(encoding="utf-8")
    assert "sections: [0.0, 4.2]" in text and "audio: _vo-fr/track-final.mp3" in text
    assert "## Lock (hand-named)" in text      # a hand-tuned shotlist keeps its names
    fresh = ep / "clip-new.md"
    E.write_clip_md(fresh, [0.0, 2.0], audio="_vo/track-final.mp3", scene="s.json", segments=2, title="t")
    assert fresh.read_text(encoding="utf-8").count("[shot: url=\"stage:s.json#") == 2


def test_shotlist_and_out_dir_follow_the_plan(ep: Path) -> None:
    assert E.shotlist_for(ep, None) == (ep / "clip-v2.md", ep / "out-v2")
    (ep / "clip-v2-fr.md").write_text("x", encoding="utf-8")
    assert E.shotlist_for(ep, {"clip": "clip-v2-fr.md"}) == (ep / "clip-v2-fr.md", ep / "out-v2-fr")


def test_a_legacy_episode_yields_a_plan_that_replaces_its_cuts(ep: Path, monkeypatch) -> None:
    vo = ep / "_vo"
    vo.mkdir()
    (vo / "build_vo.py").write_text('LINES = [\n    ("n1", "daniel", "Hi.", 0.7),\n    ("n2", "brian", "Bye.", 0.0),\n]\n',
                                    encoding="utf-8")
    (vo / "beats.json").write_text(json.dumps({"lines": [{"id": "n1", "end": 2.8, "dur": 2.8},
                                                         {"id": "n2", "end": 5.0, "dur": 1.5}]}), encoding="utf-8")
    monkeypatch.setattr(E, "_duration", lambda p: 7.0)
    cfg = E.plan_from_legacy(ep)
    assert cfg["lines"] == [["n1", "daniel", "Hi.", 0.7], ["n2", "brian", "Bye.", 0.0]]
    assert cfg["segs"] == [{"line": "n1", "hold": 0.7}, {}]       # cut 3.5 = n1 end 2.8 + 0.7
    assert E.plan_sections(cfg, [{"id": "n1", "dur": 2.8, "end": 2.8}, {"id": "n2", "dur": 1.5, "end": 5.0}]) == [0.0, 3.5]


def test_a_relative_plan_falls_back_to_the_episode_folder(ep: Path, tmp_path: Path, monkeypatch) -> None:
    plan = {"bed": "b", "length": 10, "lines": [], "segs": [{}], "speech_map": [None]}
    (ep / "rebuild.fr.json").write_text(json.dumps(plan), encoding="utf-8")
    monkeypatch.chdir(tmp_path)                    # the series folder, not the episode
    assert E.load_plan(ep, Path("rebuild.fr.json"))["bed"] == "b"
    with pytest.raises(E.EpisodeError, match="not found"):
        E.load_plan(ep, Path("rebuild.de.json"))
