"""Every command takes the short id that `list` / `gen` print, not only the full 32 chars.

`list` shows 8-character ids and says "keep/reject <id>", while every command looked the id up
by exact match, so the advice it printed could never work.
"""

from __future__ import annotations

import pytest
import typer

from navig_generate.commands import generate as cli
from navig_generate.generated_media import GeneratedMediaStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = GeneratedMediaStore(db_path=tmp_path / "generated_media.db")
    monkeypatch.setattr("navig_generate.generated_media._store", s)
    for vid in ("ab12cd34" + "0" * 24, "ab12ff99" + "1" * 24, "c0ffee00" + "2" * 24):
        s.create(id=vid, group_id="g", modality="image", prompt="p")
    return s


def test_a_unique_prefix_resolves_to_the_full_id(store):
    assert cli._full_id("c0ffee00") == "c0ffee00" + "2" * 24
    assert cli._full_id("ab12cd") == "ab12cd34" + "0" * 24


def test_the_full_id_still_works(store):
    full = "ab12ff99" + "1" * 24
    assert cli._full_id(full) == full


def test_an_ambiguous_prefix_is_refused_not_guessed(store):
    with pytest.raises(typer.Exit) as exc:
        cli._full_id("ab12")
    assert exc.value.exit_code == 1


@pytest.mark.parametrize("ref", ["zz", "c0ffee0_", "c0ff%"])
def test_no_match_and_like_wildcards_match_nothing(store, ref):
    # A literal % or _ must not act as a LIKE wildcard. Unescaped, "c0ffee0_" and "c0ff%"
    # would each match exactly ONE row and silently pick it.
    with pytest.raises(typer.Exit) as exc:
        cli._full_id(ref)
    assert exc.value.exit_code == 1
