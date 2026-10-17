"""`[shot:]` — picture cues in a scenario that is still, first and foremost, audio.

The load-bearing property is the one in :func:`test_shots_do_not_change_the_audio_at_all`:
a scenario carrying shots has to parse to exactly the same speech, the same segment
stream and the same billable character count as the same scenario without them. If that
ever drifts, adding a camera direction silently changes what the narrator says and what
the render costs — a bug you would hear before you could explain it.
"""

from __future__ import annotations

import pytest
from navig_audio.podcast.scenario import Cue, Line, ScenarioError, Shot, parse

FRONT = """---
episode: 1
title: "Reel"
lang: fr
default_speaker: NOBI
---

"""

WITHOUT = FRONT + """## 01 — Le bruit

NOBI: Avant le Réseau, le monde n'était que bruit.

## 02 — La Grille

[music: tense cyber drone, 8s]
NOBI: Puis Schema a tracé la Grille.
"""

WITH = FRONT + """## 01 — Le bruit

[shot: url="http://localhost:5002/?intro=warp" secs=3.4]
NOBI: Avant le Réseau, le monde n'était que bruit.

## 02 — La Grille

[shot: url="http://localhost:5002/map" secs=4 motion="drift"]
[music: tense cyber drone, 8s]
NOBI: Puis Schema a tracé la Grille.
"""


def _shape(episode) -> list[list[tuple[str, str]]]:
    """Every track's segment stream, reduced to (type, payload) for comparison."""
    return [
        [
            (type(s).__name__, s.text if isinstance(s, Line) else s.prompt)
            for s in track.segments
        ]
        for track in episode.tracks
    ]


# ── the invariant ─────────────────────────────────────────────────────────────


def test_shots_do_not_change_the_audio_at_all() -> None:
    bare, shot = parse(WITHOUT), parse(WITH)
    assert _shape(bare) == _shape(shot), "a shot leaked into the spoken segment stream"
    assert [t.billable_chars for t in bare.tracks] == [t.billable_chars for t in shot.tracks]
    assert [t.text for t in bare.tracks] == [t.text for t in shot.tracks]


def test_a_shot_is_never_spoken() -> None:
    episode = parse(WITH)
    spoken = " ".join(t.text for t in episode.tracks)
    assert "shot" not in spoken.lower()
    assert "localhost" not in spoken


def test_a_shot_costs_nothing() -> None:
    # Billing is per character; a camera direction that reached the API would be paid
    # for AND read aloud.
    assert parse(WITH).tracks[0].billable_chars == parse(WITHOUT).tracks[0].billable_chars


def test_music_cues_still_land_in_the_segments() -> None:
    # Shots leave; cues must not follow them out.
    track = parse(WITH).tracks[1]
    assert any(isinstance(s, Cue) and s.kind == "music" for s in track.segments)
    assert not any(isinstance(s, Shot) for s in track.segments)


# ── parsing ───────────────────────────────────────────────────────────────────


def test_shots_attach_to_their_own_track_in_order() -> None:
    episode = parse(WITH)
    assert [len(t.shots) for t in episode.tracks] == [1, 1]
    assert episode.tracks[0].shots[0].url.endswith("intro=warp")
    assert episode.tracks[0].shots[0].secs == pytest.approx(3.4)
    assert episode.tracks[1].shots[0].motion == "drift"


def test_a_url_with_query_separators_survives_unquoted() -> None:
    episode = parse(FRONT + '## 01 — X\n\n[shot: url=http://h/?a=1&b=2 secs=2]\nNOBI: Bonjour.\n')
    assert episode.tracks[0].shots[0].url == "http://h/?a=1&b=2"


def test_an_image_shot_needs_no_url() -> None:
    episode = parse(FRONT + '## 01 — X\n\n[shot: image="art/hero.png" secs=2 motion="kenburns"]\nNOBI: Bonjour.\n')
    shot = episode.tracks[0].shots[0]
    assert shot.image == "art/hero.png" and shot.url is None


def test_a_shot_with_no_source_is_refused() -> None:
    # Otherwise it renders as a silent black gap with nothing to explain it. The message
    # names all three ways to give it a source, since "no source" alone is not actionable.
    with pytest.raises(ScenarioError, match="names no source") as caught:
        parse(FRONT + "## 01 — X\n\n[shot: secs=3]\nNOBI: Bonjour.\n")
    for hint in ("url=", "prompt=", "image="):
        assert hint in str(caught.value)


@pytest.mark.parametrize("bad", ["secs=abc", "secs=0", "secs=-2"])
def test_an_unusable_duration_is_refused(bad: str) -> None:
    with pytest.raises(ScenarioError):
        parse(FRONT + f'## 01 — X\n\n[shot: url="http://h/" {bad}]\nNOBI: Bonjour.\n')


def test_a_shot_alone_between_blank_lines_does_not_break_blocking() -> None:
    # Removing the line leaves two consecutive blank lines; the empty block must be
    # dropped rather than becoming an empty utterance.
    episode = parse(
        FRONT + '## 01 — X\n\nNOBI: Un.\n\n[shot: url="http://h/" secs=1]\n\nNOBI: Deux.\n'
    )
    lines = episode.tracks[0].lines
    assert [line.text for line in lines] == ["Un.", "Deux."]


# ── generated shots (AI video) ────────────────────────────────────────────────


def test_a_prompt_shot_needs_no_url() -> None:
    episode = parse(
        FRONT + '## 01 — X\n\n[shot: prompt="a neon grid over a rain-slick street" secs=4]\nNOBI: Bonjour.\n'
    )
    shot = episode.tracks[0].shots[0]
    assert shot.prompt == "a neon grid over a rain-slick street"
    assert shot.url is None and shot.image is None
    assert shot.source == "generate"


def test_the_three_shot_kinds_are_distinguishable() -> None:
    episode = parse(
        FRONT
        + '## 01 — X\n\n[shot: url="http://h/" secs=1]\n[shot: prompt="a city" secs=1]\n'
          '[shot: image="a.png" secs=1]\nNOBI: Bonjour.\n'
    )
    assert [s.source for s in episode.tracks[0].shots] == ["capture", "generate", "still"]


def test_a_generated_shot_is_still_never_spoken_or_billed() -> None:
    # The whole point of keeping shots off the segment stream — a prompt is a camera
    # direction, and reading it aloud would be both wrong and paid for.
    with_prompt = parse(
        FRONT + '## 01 — X\n\n[shot: prompt="a lone figure walking" secs=4]\nNOBI: Bonjour.\n'
    )
    bare = parse(FRONT + "## 01 — X\n\nNOBI: Bonjour.\n")
    assert with_prompt.tracks[0].billable_chars == bare.tracks[0].billable_chars
    assert "lone figure" not in with_prompt.tracks[0].text


def test_a_provider_override_is_carried() -> None:
    episode = parse(
        FRONT + '## 01 — X\n\n[shot: prompt="a city" provider="replicate" secs=2]\nNOBI: Bonjour.\n'
    )
    assert episode.tracks[0].shots[0].provider == "replicate"


def test_capturing_and_generating_the_same_shot_is_refused() -> None:
    # Two answers to one question; silently preferring one wastes whichever was meant.
    with pytest.raises(ScenarioError, match="both"):
        parse(
            FRONT + '## 01 — X\n\n[shot: url="http://h/" prompt="a city" secs=2]\nNOBI: Bonjour.\n'
        )
