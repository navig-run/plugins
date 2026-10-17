"""Pronunciation lexicon — names said right per language, the screen untouched."""

from __future__ import annotations

import pytest

from navig_audio.voice import lexicon


@pytest.fixture(autouse=True)
def _no_user_config(monkeypatch):
    monkeypatch.setattr(lexicon, "user_entries", lambda lang: {})


@pytest.mark.parametrize(
    ("lang", "said"),
    [("en", "Skeema deployed."), ("fr", "schéma deployed."), ("ru", "Схема deployed.")],
)
def test_schema_is_said_the_way_each_language_says_it(lang, said):
    assert lexicon.apply("SCHEMA deployed.", lang) == said


def test_any_case_matches_but_only_whole_words():
    assert lexicon.apply("Schema and schema", "en") == "Skeema and Skeema"
    assert lexicon.apply("a schematic of schemas", "en") == "a schematic of schemas"


def test_the_longer_dotted_name_wins():
    assert lexicon.apply("visit schema.cx now", "en") == "visit Skeema dot C X now"


def test_a_regional_tag_uses_its_language():
    assert lexicon.apply("SCHEMA", "fr-FR") == "schéma"


def test_an_unknown_or_empty_language_leaves_the_text_alone():
    assert lexicon.apply("SCHEMA", "de") == "SCHEMA"
    assert lexicon.apply("SCHEMA", None) == "SCHEMA"


def test_user_entries_override_the_builtin(monkeypatch):
    monkeypatch.setattr(lexicon, "user_entries", lambda lang: {"SCHEMA": "Shema"})
    assert lexicon.apply("SCHEMA", "en") == "Shema"


def test_extra_entries_win_over_everything():
    assert lexicon.apply("Grid", "en", {"grid": "the Grid"}) == "the Grid"
