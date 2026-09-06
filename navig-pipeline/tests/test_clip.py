"""Cutting picture to a track that already exists.

`reel` fits picture to narration it renders; `clip` fits picture to audio the user hands
it. The invariant is the same and is the whole point — whatever the author wrote, the
sections must total EXACTLY the length of the song. A clip that ends early shows black
under the outro; one that ends late gets its tail cut by the encoder. Both are discovered
on playback rather than on render, which is why they are pinned here.

Everything tested is pure: no ffmpeg, no network, no browser.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from navig_pipeline.clip import (
    SECTION_EPSILON_S,
    ClipError,
    cuts_of,
    detect_capabilities,
    generated_count,
    needs_capture,
    plan_sections,
    resolve_audio,
    resolve_look_for,
    resolve_video_settings,
    snap,
    thin,
)


@dataclass(frozen=True)
class FakeShot:
    """Stands in for navig_audio.podcast.scenario.Shot — same duck type, no dependency."""

    url: str | None = None
    image: str | None = "art/x.png"
    prompt: str | None = None
    provider: str | None = None
    secs: float | None = None
    motion: str = "none"


@dataclass
class FakeTrack:
    number: int
    title: str = "section"
    shots: list = field(default_factory=list)
    segments: list = field(default_factory=list)

    @property
    def lines(self):
        return [s for s in self.segments if getattr(s, "is_speech", False)]

    @property
    def cues(self):
        return [s for s in self.segments if not getattr(s, "is_speech", True)]


@dataclass
class FakeEpisode:
    tracks: list
    meta: dict = field(default_factory=dict)


def episode(*shot_groups, meta=None):
    return FakeEpisode(
        tracks=[FakeTrack(number=i + 1, shots=list(g)) for i, g in enumerate(shot_groups)],
        meta=dict(meta or {}),
    )


# -- the invariant ------------------------------------------------------------


@pytest.mark.parametrize("audio_s", [12.538776, 63.21, 139.668027, 4.0])
@pytest.mark.parametrize("written", [
    [[1.0], [1.0]],
    [[3.2], [3.2], [3.0], [3.1]],
    [[1.0, 2.0], [4.0]],
    [[None], [None], [None]],
])
def test_sections_always_add_up_to_the_track(written, audio_s):
    ep = episode(*[[FakeShot(secs=s) for s in group] for group in written])
    sections, _ = plan_sections(ep, audio_s)
    assert sum(s.seconds for s in sections) == pytest.approx(audio_s, abs=1e-6)
    # ...and so does every shot inside them: picture covers the song end to end.
    total = sum(shot.seconds for s in sections for shot in s.shots)
    assert total == pytest.approx(audio_s, abs=1e-6)


def test_sections_are_contiguous():
    ep = episode([FakeShot(secs=1)], [FakeShot(secs=2)], [FakeShot(secs=1)])
    sections, _ = plan_sections(ep, 12.0)
    assert sections[0].start == 0.0
    for previous, following in zip(sections, sections[1:]):
        assert previous.end == pytest.approx(following.start, abs=1e-9)


def test_unpinned_sections_split_by_written_weight():
    # Written 1:2:1 across 12s must come out 3:6:3.
    ep = episode([FakeShot(secs=1)], [FakeShot(secs=2)], [FakeShot(secs=1)])
    sections, _ = plan_sections(ep, 12.0)
    assert [round(s.seconds, 3) for s in sections] == [3.0, 6.0, 3.0]
    assert all(not s.pinned for s in sections)


def test_a_section_with_no_written_secs_is_weighted_by_shot_count():
    # Two shots of unstated length is twice as much picture as one, not an equal split.
    ep = episode([FakeShot(), FakeShot()], [FakeShot()])
    sections, _ = plan_sections(ep, 9.0)
    assert [round(s.seconds, 3) for s in sections] == [6.0, 3.0]


# -- pinned cuts --------------------------------------------------------------


def test_pinned_sections_land_on_the_written_seconds():
    ep = episode([FakeShot()], [FakeShot()], [FakeShot()], [FakeShot()],
                 meta={"sections": [0, 3.2, 6.4, 9.4]})
    sections, _ = plan_sections(ep, 12.538776)
    assert [round(s.start, 3) for s in sections] == [0.0, 3.2, 6.4, 9.4]
    assert [round(s.seconds, 3) for s in sections] == [3.2, 3.2, 3.0, 3.139]
    assert all(s.pinned for s in sections)
    assert sum(s.seconds for s in sections) == pytest.approx(12.538776, abs=1e-6)


def test_a_pinned_list_that_does_not_match_the_sections_is_an_error():
    ep = episode([FakeShot()], [FakeShot()], meta={"sections": [0, 2, 4]})
    with pytest.raises(ClipError, match="line up one to one"):
        plan_sections(ep, 10.0)


def test_pinned_starts_must_ascend():
    ep = episode([FakeShot()], [FakeShot()], [FakeShot()], meta={"sections": [0, 5, 3]})
    with pytest.raises(ClipError, match="must ascend"):
        plan_sections(ep, 10.0)


def test_pinned_starts_must_begin_at_zero():
    ep = episode([FakeShot()], [FakeShot()], meta={"sections": [1.5, 5]})
    with pytest.raises(ClipError, match="must start at 0"):
        plan_sections(ep, 10.0)


def test_a_pinned_start_past_the_end_of_the_track_is_an_error():
    ep = episode([FakeShot()], [FakeShot()], meta={"sections": [0, 11]})
    with pytest.raises(ClipError, match="no time on screen"):
        plan_sections(ep, 10.0)


def test_a_rounding_sized_first_start_is_tolerated_not_rejected():
    # A boundary written as 0.05 is rounding, not a mistake — it is snapped, not refused.
    ep = episode([FakeShot()], [FakeShot()],
                 meta={"sections": [SECTION_EPSILON_S / 2, 5]})
    sections, _ = plan_sections(ep, 10.0)
    assert sections[0].start == 0.0


def test_pinned_sections_must_be_numbers():
    ep = episode([FakeShot()], meta={"sections": "0,3,6"})
    with pytest.raises(ClipError, match="must be a list"):
        plan_sections(ep, 10.0)


# -- what a clip refuses, and what it merely warns about ----------------------


def test_a_section_with_no_shots_is_refused():
    ep = episode([FakeShot()], [])
    with pytest.raises(ClipError, match="no shots"):
        plan_sections(ep, 10.0)


def test_a_shotlist_with_no_sections_is_refused():
    with pytest.raises(ClipError, match="no section headings"):
        plan_sections(FakeEpisode(tracks=[]), 10.0)


def test_a_section_squeezed_below_the_floor_is_refused():
    # Four pinned sections across 1s cannot each be a shot anyone can read.
    ep = episode(*[[FakeShot()] for _ in range(4)], meta={"sections": [0, 0.2, 0.4, 0.6]})
    with pytest.raises(ClipError, match="flash frame"):
        plan_sections(ep, 1.0)


@dataclass(frozen=True)
class FakeCue:
    is_speech: bool = False


@dataclass(frozen=True)
class FakeLine:
    is_speech: bool = True


def test_music_cues_are_reported_not_silently_dropped():
    ep = episode([FakeShot()])
    ep.tracks[0].segments = [FakeCue()]
    _, notes = plan_sections(ep, 10.0)
    assert any("cues are ignored" in note for note in notes)


def test_speech_in_a_shotlist_is_reported_never_billed():
    ep = episode([FakeShot()])
    ep.tracks[0].segments = [FakeLine()]
    _, notes = plan_sections(ep, 10.0)
    assert any("does not narrate" in note for note in notes)


def test_missing_audio_says_so_before_anything_is_generated(tmp_path):
    ep = FakeEpisode(tracks=[], meta={})
    with pytest.raises(ClipError, match="no audio"):
        resolve_audio(tmp_path / "s.md", ep)
    ep.meta["audio"] = "nope.mp3"
    with pytest.raises(ClipError, match="audio not found"):
        resolve_audio(tmp_path / "s.md", ep)


def test_declared_audio_resolves_relative_to_the_shotlist(tmp_path):
    (tmp_path / "songs").mkdir()
    track = tmp_path / "songs" / "mix.mp3"
    track.write_bytes(b"not really audio")
    shotlist = tmp_path / "list" / "clip.md"
    shotlist.parent.mkdir()
    ep = FakeEpisode(tracks=[], meta={"audio": "../songs/mix.mp3"})
    assert resolve_audio(shotlist, ep) == track.resolve()


# -- the browser, and the bill ------------------------------------------------


def test_a_clip_that_films_nothing_asks_for_no_browser():
    # The reason this exists: `reel` launches Chrome for every scenario, generated or not,
    # and a browser nobody asked for is a browser nobody closes.
    ep = episode([FakeShot(image="a.png")], [FakeShot(prompt="a red forest")])
    assert needs_capture(ep) is False
    assert "capture" not in detect_capabilities(ep)


def test_a_clip_with_a_url_shot_does_ask_for_a_browser():
    ep = episode([FakeShot(url="http://localhost:5002/", image=None)])
    assert needs_capture(ep) is True
    assert "capture" in detect_capabilities(ep)


def test_only_generated_shots_are_counted_as_spend():
    ep = episode(
        [FakeShot(image="a.png"), FakeShot(prompt="a mask in the dark")],
        [FakeShot(prompt="a flare behind the trees")],
    )
    sections, _ = plan_sections(ep, 12.0)
    assert generated_count(sections) == 2


def test_cuts_are_the_section_seams_not_every_shot():
    ep = episode([FakeShot(secs=1), FakeShot(secs=1)], [FakeShot(secs=2)])
    sections, _ = plan_sections(ep, 8.0)
    # Three shots, but only one change of subject — so exactly one cut to fire an effect on.
    assert cuts_of(sections) == [pytest.approx(4.0)]


# -- the grade ----------------------------------------------------------------


def test_a_project_brings_its_own_look_without_joining_the_builtin_set():
    # The built-in registry belongs to one universe and its tests say so. A song from a
    # different one gets a grade by carrying the numbers, not by being added to that set.
    from navig_pipeline.looks import LOOKS

    ep = episode([FakeShot()], meta={"look_spec": {
        "name": "flare", "description": "a road flare in a night wood",
        "accent": "0xD62828", "grain": 9.0, "vignette": 0.5, "glitch_strength": 6,
    }})
    look = resolve_look_for(ep)
    assert look.name == "flare"
    assert "colorhold" in look.picture_filter()
    assert "flare" not in LOOKS, "an inline look must never leak into the built-in registry"


def test_a_named_builtin_look_still_resolves():
    ep = episode([FakeShot()], meta={"look": "broadcast"})
    assert resolve_look_for(ep).name == "broadcast"


def test_the_command_line_look_overrides_the_shotlist():
    ep = episode([FakeShot()], meta={"look": "broadcast"})
    assert resolve_look_for(ep, "operator").name == "operator"


def test_naming_both_a_look_and_a_spec_is_an_error_not_a_precedence_rule():
    ep = episode([FakeShot()], meta={"look": "broadcast", "look_spec": {"name": "x"}})
    with pytest.raises(ClipError, match="use one"):
        resolve_look_for(ep)


def test_a_typo_in_a_look_spec_is_refused_not_ignored():
    # A silently dropped setting renders a video that merely looks weak — the hardest
    # kind of mistake to notice.
    ep = episode([FakeShot()], meta={"look_spec": {"name": "x", "grian": 9.0}})
    with pytest.raises(ClipError, match="grian"):
        resolve_look_for(ep)


def test_an_unknown_named_look_is_refused():
    ep = episode([FakeShot()], meta={"look": "nope"})
    with pytest.raises(ClipError, match="bad `look:`"):
        resolve_look_for(ep)


# -- what the warnings actually say -------------------------------------------


def test_a_clip_never_warns_about_narration_it_does_not_have():
    # plan_shots is shared with reel, whose wording is all voice and "spoken". A warning
    # naming the wrong thing is worse than none: it sends the reader hunting for narration.
    ep = episode([FakeShot(secs=1)], [FakeShot(secs=1)], meta={"sections": [0, 5]})
    _, notes = plan_sections(ep, 40.0)
    joined = " ".join(notes).lower()
    assert notes, "a 4x stretch inside a PINNED section is worth saying"
    assert "spoken" not in joined
    assert "the voice" not in joined
    assert "the track" in joined


def test_an_unpinned_shotlist_does_not_warn_once_per_section():
    # Unpinned sections are all scaled by the SAME factor, so a per-section stretch
    # warning says only "your relative numbers were relative" — nine times over.
    ep = episode(*[[FakeShot(secs=6.0)] for _ in range(9)])
    _, notes = plan_sections(ep, 139.668027)
    assert notes == []


# -- naming a different video model -------------------------------------------


def test_a_shot_with_both_an_image_and_a_prompt_is_image_to_video():
    # Not a still and not an invention: THIS frame, moved this way. It is the answer to a
    # text-to-video model building its own world when the art direction is already settled.
    from navig_pipeline.reel import plan_shots

    planned, _ = plan_shots([FakeShot(image="art/hero.png", prompt="slow push in")], 4.0)
    assert planned[0].source == "generate"
    assert planned[0].image == "art/hero.png"
    assert planned[0].prompt == "slow push in"


def test_a_shotlist_can_name_its_own_model_and_that_models_inputs():
    ep = episode([FakeShot()], meta={
        "video_model": "wan-video/wan-2.2-i2v-fast",
        "video_input": {"resolution": "480p"},
        "video_image_key": "image",
    })
    assert resolve_video_settings(ep) == (
        "wan-video/wan-2.2-i2v-fast", {"resolution": "480p"}, "image")


def test_the_command_line_model_beats_the_shotlist():
    ep = episode([FakeShot()], meta={"video_model": "kwaivgi/kling-v2.1"})
    assert resolve_video_settings(ep, "wan-video/wan-2.2-t2v-fast")[0] == "wan-video/wan-2.2-t2v-fast"


def test_video_input_must_be_a_mapping_not_a_string():
    # `video_input:` sat in a comment for months, read by nothing. Now that it IS read, a
    # scalar has to fail loudly — silently dropping it sends the wrong inputs to the model.
    ep = episode([FakeShot()], meta={"video_input": "aspect_ratio=9:16"})
    with pytest.raises(ClipError, match="must be a mapping"):
        resolve_video_settings(ep)


def test_no_video_settings_means_the_builtin_defaults():
    assert resolve_video_settings(episode([FakeShot()])) == (None, None, None)


# -- the seam itself, without spending anything -------------------------------


def test_capture_uploads_the_still_and_hands_the_model_its_url(tmp_path, monkeypatch):
    """A shot with image+prompt must actually SEED the model, not quietly drop the image.

    _capture ignored `image=` whenever `prompt=` was present, so the pair parsed fine and
    then produced text-to-video anyway — a silent downgrade that only showed up as the
    model inventing its own world.
    """
    import asyncio

    from navig.media import video_edit
    from navig.tools import video_generation as vg
    from navig_pipeline.reel import PlannedShot, _capture

    seed = tmp_path / "hero.png"
    seed.write_bytes(b"\x89PNG\r\n\x1a\n")
    seen = {}

    class FakeProduced:
        local_path = str(tmp_path / "raw.mp4")

    class FakeGenerator:
        def __init__(self, config):
            seen["config"] = config

        async def upload_image(self, path):
            seen["uploaded"] = Path(path).name
            return "https://replicate.delivery/hero.png"

        async def generate(self, prompt, *, image_url=None, provider=None, extra_input=None):
            seen["image_url"] = image_url
            seen["extra_input"] = extra_input
            return FakeProduced()

        async def close(self):
            seen["closed"] = True

    monkeypatch.setattr(vg, "VideoGenerator", FakeGenerator)
    monkeypatch.setattr(video_edit, "to_vertical", lambda *a, **k: None)
    monkeypatch.setattr(video_edit, "fit_duration", lambda *a, **k: None)

    shot = PlannedShot(index=0, seconds=3.2, image=str(seed), prompt="slow push in")
    asyncio.run(_capture(
        shot, tmp_path / "out.mp4", port=0, width=1080, height=1920, fps=30,
        base_dir=tmp_path, progress=lambda _m: None,
        model="wan-video/wan-2.2-i2v-fast",
        image_key="image", extra_input={"resolution": "720p"},
    ))

    assert seen["uploaded"] == "hero.png"
    assert seen["image_url"] == "https://replicate.delivery/hero.png"
    # video_input REPLACES the vertical defaults: an i2v model has no aspect_ratio and
    # Replicate rejects an input it never declared.
    assert seen["extra_input"] == {"resolution": "720p"}
    assert "aspect_ratio" not in seen["extra_input"]
    assert seen["config"].replicate_model == "wan-video/wan-2.2-i2v-fast"
    assert seen["config"].replicate_image_key == "image"


def test_capture_without_a_seed_image_still_asks_for_vertical(tmp_path, monkeypatch):
    import asyncio

    from navig.media import video_edit
    from navig.tools import video_generation as vg
    from navig_pipeline.reel import VERTICAL_INPUT, PlannedShot, _capture

    seen = {}

    class FakeGenerator:
        def __init__(self, config): pass
        async def generate(self, prompt, *, image_url=None, provider=None, extra_input=None):
            seen["image_url"], seen["extra_input"] = image_url, extra_input
            return type("P", (), {"local_path": str(tmp_path / "r.mp4")})()
        async def close(self): pass

    monkeypatch.setattr(vg, "VideoGenerator", FakeGenerator)
    monkeypatch.setattr(video_edit, "to_vertical", lambda *a, **k: None)
    monkeypatch.setattr(video_edit, "fit_duration", lambda *a, **k: None)

    asyncio.run(_capture(
        PlannedShot(index=0, seconds=3.0, prompt="a red forest"), tmp_path / "o.mp4",
        port=0, width=1080, height=1920, fps=30, base_dir=tmp_path,
        progress=lambda _m: None,
    ))
    assert seen["image_url"] is None
    assert seen["extra_input"] == VERTICAL_INPUT


# -- reusing footage instead of paying for it twice ---------------------------


def test_an_image_shot_pointing_at_a_clip_is_reframed_not_held(tmp_path, monkeypatch):
    """`image=` with a video extension means FOOTAGE, and that is what makes a re-grade free.

    Generated shots are the only expensive thing in this pipeline. Without this, changing
    a look or re-cutting to the beat meant paying the video model again for footage already
    sitting on disk.
    """
    import asyncio

    from navig.media import video_edit
    from navig_pipeline.reel import PlannedShot, _capture

    clip = tmp_path / "already-paid-for.mp4"
    clip.write_bytes(b"\x00")
    seen = {}
    monkeypatch.setattr(video_edit, "to_vertical", lambda src, dst, **k: seen.update(vertical=src.name))
    monkeypatch.setattr(video_edit, "fit_duration", lambda src, dst, **k: seen.update(fit=k.get("secs")))
    monkeypatch.setattr(video_edit, "still", lambda *a, **k: seen.update(still=True))

    asyncio.run(_capture(
        PlannedShot(index=0, seconds=2.6, image=str(clip)), tmp_path / "out.mp4",
        port=0, width=1080, height=1920, fps=30, base_dir=tmp_path,
        progress=lambda _m: None,
    ))
    assert seen["vertical"] == "already-paid-for.mp4"
    assert seen["fit"] == 2.6
    assert "still" not in seen, "a clip must not be frozen as a still"


def test_an_image_shot_pointing_at_a_png_is_still_a_still(tmp_path, monkeypatch):
    import asyncio

    from navig.media import video_edit
    from navig_pipeline.reel import PlannedShot, _capture

    art = tmp_path / "hero.png"
    art.write_bytes(b"\x89PNG")
    seen = {}
    monkeypatch.setattr(video_edit, "still", lambda src, dst, **k: seen.update(still=src.name, secs=k.get("secs")))
    monkeypatch.setattr(video_edit, "to_vertical", lambda *a, **k: seen.update(vertical=True))

    asyncio.run(_capture(
        PlannedShot(index=0, seconds=3.0, image=str(art), motion="kenburns"),
        tmp_path / "o.mp4", port=0, width=1080, height=1920, fps=30,
        base_dir=tmp_path, progress=lambda _m: None,
    ))
    assert seen["still"] == "hero.png"
    assert "vertical" not in seen


# -- cutting to the beat ------------------------------------------------------


class FakeGrid:
    bpm = 92.25
    confidence = 0.8

    def __init__(self, beats, downbeats):
        self.beats, self.downbeats = beats, downbeats


def test_section_cuts_move_onto_downbeats():
    # Written pacing says 1:1, so the seam wants 5.0s; the nearest downbeat is 5.2s.
    ep = episode([FakeShot(secs=1)], [FakeShot(secs=1)])
    grid = FakeGrid(beats=[i * 0.65 for i in range(16)], downbeats=[0.0, 2.6, 5.2, 7.8])
    sections, notes = plan_sections(ep, 10.0, grid)
    assert sections[1].start == pytest.approx(5.2)
    assert any("snapped to the beat" in n for n in notes)
    # ...and the picture still ends exactly with the track.
    assert sum(s.seconds for s in sections) == pytest.approx(10.0)


def test_shots_inside_a_section_move_onto_beats():
    ep = episode([FakeShot(secs=1), FakeShot(secs=1)])
    grid = FakeGrid(beats=[0.0, 0.65, 1.3, 1.95, 2.6, 3.25], downbeats=[0.0, 2.6])
    sections, _ = plan_sections(ep, 4.0, grid)
    first = sections[0].shots[0].seconds
    assert first == pytest.approx(1.95), "the internal cut should land on a beat, not at 2.0"
    assert sum(s.seconds for s in sections[0].shots) == pytest.approx(4.0)


def test_beat_sync_can_be_turned_off():
    ep = episode([FakeShot(secs=1)], [FakeShot(secs=1)], meta={"beat_sync": False})
    grid = FakeGrid(beats=[0.0, 0.65], downbeats=[0.0, 5.2])
    sections, notes = plan_sections(ep, 10.0, grid)
    assert sections[1].start == pytest.approx(5.0)
    assert not any("snapped" in n for n in notes)


def test_effect_hits_are_thinned_evenly_not_truncated():
    # An effect that fires for the first thirty seconds and then stops looks broken.
    many = [i * 0.65 for i in range(400)]
    kept = thin(many, limit=96)
    assert len(kept) <= 96
    assert kept[0] == pytest.approx(0.0)
    assert kept[-1] > many[-1] * 0.9, "thinning must span the whole track"


def test_snapping_never_reorders_or_crushes_a_shot():
    starts = [0.0, 1.0, 2.0, 3.0]
    # A pathological grid that would collapse every boundary onto one point.
    moved = snap(starts, [2.999, 3.0], total=4.0, min_gap=0.35)
    assert moved == sorted(moved)
    for a, b in zip(moved, moved[1:]):
        assert b - a >= 0.35 - 1e-9
