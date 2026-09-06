"""Scenario parsing — what counts as speech, and what must never be spoken.

The costly failure this format exists to prevent is a production note read aloud in a
finished episode, so most of these tests are about exclusion rather than inclusion.
"""

from __future__ import annotations

import pytest

from navig_audio.podcast import scenario as sc

STRICT = """---
episode: 0
slug: ouverture-du-terminal
title: "NobiCast EP0 — Ouverture du Terminal"
lang: fr
default_speaker: NOBI
voices:
  NOBI: {voice_id: "abc123", stability: 0.5}
  SERIO: "def456"
---

## 01 — Intro & Sonic Branding

[music: cyberpunk modem swell, warm, 8s]
[sfx: 56k handshake]

NOBI: Connexion établie. Terminal en ligne.
Bienvenue dans NobiCast.

> a production note that must never be spoken

- a bullet talking point, also never spoken

## 02 — Présentation des animateurs

SERIO: Salut tout le monde, ici Serio.
NOBI: Et moi je suis Nobi.

Une phrase sans locuteur explicite.

<!-- an editorial comment -->
"""


@pytest.fixture
def episode() -> sc.Episode:
    return sc.parse(STRICT)


def test_frontmatter_is_read(episode: sc.Episode) -> None:
    assert episode.number == 0
    assert episode.lang == "fr"
    assert episode.default_speaker == "NOBI"
    assert episode.title == "NobiCast EP0 — Ouverture du Terminal"
    assert episode.dirname == "ep000-ouverture-du-terminal"


def test_headings_become_numbered_tracks(episode: sc.Episode) -> None:
    assert [t.number for t in episode.tracks] == [1, 2]
    assert episode.tracks[0].title == "Intro & Sonic Branding"
    # Accents are folded, because track slugs become filenames.
    assert episode.tracks[1].slug == "presentation-des-animateurs"
    assert episode.tracks[0].stem() == "01-intro-sonic-branding"


def test_notes_are_never_spoken(episode: sc.Episode) -> None:
    spoken = " ".join(line.text for track in episode.tracks for line in track.lines)
    assert "production note" not in spoken
    assert "bullet talking point" not in spoken
    assert "editorial comment" not in spoken


def test_notes_are_not_billed(episode: sc.Episode) -> None:
    # The cost of an episode must reflect only what is sent to the provider.
    assert episode.tracks[0].billable_chars == len(
        "Connexion établie. Terminal en ligne. Bienvenue dans NobiCast."
    )


def test_wrapped_paragraph_stays_one_utterance(episode: sc.Episode) -> None:
    lines = episode.tracks[0].lines
    assert len(lines) == 1
    assert lines[0].text.endswith("Bienvenue dans NobiCast.")


def test_speaker_tags_switch_voice_without_blank_lines(episode: sc.Episode) -> None:
    # Two speaker tags in one block are two utterances, not one line that reads the
    # other speaker's name aloud.
    lines = episode.tracks[1].lines
    assert (lines[0].speaker, lines[0].text) == ("SERIO", "Salut tout le monde, ici Serio.")
    assert (lines[1].speaker, lines[1].text) == ("NOBI", "Et moi je suis Nobi.")
    assert "SERIO:" not in lines[0].text


def test_bare_paragraph_uses_the_default_speaker(episode: sc.Episode) -> None:
    assert episode.tracks[1].lines[-1].speaker == "NOBI"


def test_cues_are_captured_in_reading_order(episode: sc.Episode) -> None:
    segments = episode.tracks[0].segments
    assert isinstance(segments[0], sc.Cue) and segments[0].kind == "music"
    assert isinstance(segments[1], sc.Cue) and segments[1].kind == "sfx"
    assert isinstance(segments[2], sc.Line)
    assert segments[0].duration_s == 8.0
    assert segments[1].duration_s is None


def test_voice_map_accepts_both_forms(episode: sc.Episode) -> None:
    assert episode.voice_for("NOBI").voice_id == "abc123"
    assert episode.voice_for("NOBI").settings == {"stability": 0.5}
    assert episode.voice_for("SERIO").voice_id == "def456"


def test_unknown_speaker_names_what_to_add(episode: sc.Episode) -> None:
    with pytest.raises(sc.ScenarioError, match="GUEST"):
        episode.voice_for("GUEST")


def test_missing_voice_id_is_reported_but_still_parses() -> None:
    # Planning must work before you have a clone; only rendering needs the id.
    parsed = sc.parse(
        "---\ndefault_speaker: HOST\nvoices:\n  HOST: \"\"\n---\n## 01 — X\nHOST: Bonjour.\n"
    )
    assert parsed.unvoiced_speakers() == ["HOST"]


def test_roundtrip_through_dump_is_stable(episode: sc.Episode) -> None:
    # Translation writes files with dump() and renders them with parse(); a lossy
    # roundtrip would mean a translated episode that will not build.
    again = sc.parse(sc.dump(episode))

    def shape(ep: sc.Episode) -> list:
        return [
            (
                t.number,
                t.title,
                [
                    (s.speaker, s.text) if isinstance(s, sc.Line) else (s.kind, s.prompt)
                    for s in t.segments
                ],
            )
            for t in ep.tracks
        ]

    assert shape(again) == shape(episode)
    assert again.voices == episode.voices


def test_no_headings_is_an_actionable_error() -> None:
    with pytest.raises(sc.ScenarioError, match="draft"):
        sc.parse("---\nlang: fr\n---\n\nJuste du texte sans titre.\n")


def test_duplicate_track_numbers_are_refused() -> None:
    # Two tracks numbered 01 would write to the same filename, silently losing one.
    with pytest.raises(sc.ScenarioError, match="numbered 1"):
        sc.parse("---\nlang: fr\n---\n## 01 — A\nHOST: a\n\n## 01 — B\nHOST: b\n")


def test_unclosed_frontmatter_says_so() -> None:
    with pytest.raises(sc.ScenarioError, match="never closes"):
        sc.parse("---\nlang: fr\n\n## 01 — A\nHOST: a\n")


def test_colon_prefixed_prose_is_not_a_speaker() -> None:
    # "Note:" is ordinary writing; only ALL-CAPS tags switch voice.
    parsed = sc.parse("---\nlang: en\n---\n## 01 — A\nNote: this is still speech.\n")
    assert parsed.tracks[0].lines[0].text == "Note: this is still speech."


def test_slugify_folds_accents_and_punctuation() -> None:
    assert sc.slugify("L'Épisode Zéro !") == "l-episode-zero"
    assert sc.slugify("???", fallback="track") == "track"
