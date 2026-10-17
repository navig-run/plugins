"""Localized scenes — words translated, captures and window bars untouched."""

from __future__ import annotations

import pytest

from navig_pipeline.localize import LocalizeError, localize

SCENE = {"title": "e07", "segments": [
    {"type": "hook", "title": "My AI agent tried to *rm -rf prod*", "sub": "Two guards."},
    {"type": "terminal", "title": "~ prod-01", "cast": "2-rm.cast", "command": "navig run 'rm -rf /var/www'",
     "caption": "A human decides.",
     "speech": {"cues": [["Then it asked to delete production files.", 0.5, 3.0]], "words": [["Then", 0.5, 0.7]]}},
    {"type": "outro", "cta": "pipx install navig", "follow": "Follow for one command a day"},
]}


def _fake(texts: list[str], lang: str) -> list[str]:
    return [f"[{lang}] {t}" for t in texts]


def test_reads_are_translated_and_captures_are_not() -> None:
    out, counts = localize(SCENE, "fr", _fake)
    hook, term, outro = out["segments"]
    assert hook["title"].startswith("[fr] ") and term["caption"] == "[fr] A human decides."
    assert term["title"] == "~ prod-01"                       # a window bar, not prose
    assert term["command"] == SCENE["segments"][1]["command"]  # real capture: never rewritten
    assert outro["cta"] == "pipx install navig"
    assert out["lang"] == "fr" and counts == {"strings": 0, "model": 5}
    assert SCENE["segments"][0]["title"].startswith("My AI")  # the source is not mutated


def test_cues_keep_their_window_and_words_spread_across_it() -> None:
    out, _ = localize(SCENE, "fr", _fake)
    cue = out["segments"][1]["speech"]["cues"][0]
    words = out["segments"][1]["speech"]["words"]
    assert cue[1:] == [0.5, 3.0]
    assert [w[0] for w in words] == cue[0].split()
    assert words[0][1] == 0.5 and words[-1][2] == 3.0


def test_reviewed_strings_are_used_first_and_only_gaps_go_to_the_model() -> None:
    seen: list[str] = []

    def spy(texts: list[str], lang: str) -> list[str]:
        seen.extend(texts)
        return _fake(texts, lang)

    out, counts = localize(SCENE, "fr", spy, strings={"Two guards.": "Deux gardes."})
    assert out["segments"][0]["sub"] == "Deux gardes." and "Two guards." not in seen
    assert counts["strings"] == 1


def test_a_translator_that_drops_lines_is_refused() -> None:
    with pytest.raises(LocalizeError, match="returned"):
        localize(SCENE, "fr", lambda texts, lang: texts[:-1])
