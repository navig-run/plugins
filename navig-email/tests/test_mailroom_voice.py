"""Styles and identities: the resolution order, and the facts that outrank the persona.

The risk this guards is specific. A persona prompt ("write like Steve Jobs") is an instruction to
a model to behave like someone else; the identity block is the set of facts about who the sender
actually is. If the persona wins, the model signs a studio's mail as a billionaire, or invents a
company. So the order in `build_prompt` is itself the contract, and it is asserted here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from navig_email import voice as V  # noqa: E402
from navig_email.space import MailroomPaths  # noqa: E402

IDENTITIES = """
version: 1
default: house
identities:
  house:
    display: Sam
    company: House Studio
    context: A small studio.
    match:
      accounts: [box@gmail.com]
      domains: [house.test]
      aliases: [support, hello]
  dev:
    display: Sam
    company: dev account
    context: Engineer-to-engineer.
    match:
      accounts: [dev@gmail.com]
  private:
    display: Sam
    language: French
    context: Personal administration.
    match:
      aliases: [sam]
"""


@pytest.fixture
def paths(tmp_path) -> MailroomPaths:
    root = tmp_path / "space"
    (root / ".navig").mkdir(parents=True)
    (root / "mailroom" / "styles").mkdir(parents=True)
    (root / ".navig" / "config.yaml").write_text("mailroom: {}\n", encoding="utf-8")
    (root / "mailroom" / "identities.yaml").write_text(IDENTITIES, encoding="utf-8")
    return MailroomPaths(root)


# ── styles ──────────────────────────────────────────────────────────────────


def test_the_shipped_styles_are_all_loadable(paths):
    table = V.available_styles(paths)
    for expected in ("plain", "brief", "jobs", "musk", "firm"):
        assert expected in table, f"{expected} is not shipped"
        text, _ = V.load_style(expected, paths)
        assert len(text) > 80


def test_a_space_style_shadows_a_builtin_of_the_same_name(paths):
    (paths.base / "styles" / "plain.md").write_text("MINE", encoding="utf-8")
    text, src = V.load_style("plain", paths)
    assert text == "MINE"
    assert src.is_relative_to(paths.base)
    # and a name the space does not define still resolves to the built-in
    assert V.load_style("jobs", paths)[0] != "MINE"


def test_an_unknown_style_names_the_ones_that_exist(paths):
    with pytest.raises(V.VoiceError) as exc:
        V.load_style("shakespeare", paths)
    assert "shakespeare" in str(exc.value) and "plain" in str(exc.value)


def test_an_empty_style_file_is_refused_not_silently_used(paths):
    (paths.base / "styles" / "hollow.md").write_text("   \n", encoding="utf-8")
    with pytest.raises(V.VoiceError):
        V.load_style("hollow", paths)


# ── identity resolution ─────────────────────────────────────────────────────


def test_the_alias_decides_before_the_account(paths):
    """One mailbox receives every alias, so the account alone cannot tell them apart."""
    ident = V.resolve_identity(paths, account="box@gmail.com", alias="sam")
    assert ident and ident.name == "private"


def test_the_account_decides_when_there_is_no_alias(paths):
    assert V.resolve_identity(paths, account="dev@gmail.com").name == "dev"


def test_the_recipient_domain_is_used_before_the_account(paths):
    ident = V.resolve_identity(paths, account="dev@gmail.com", to="anything@house.test")
    assert ident and ident.name == "house"


def test_an_unmatched_message_falls_back_to_the_default(paths):
    assert V.resolve_identity(paths, account="nobody@elsewhere.test").name == "house"


def test_an_explicit_name_wins_and_a_wrong_one_is_refused(paths):
    assert V.resolve_identity(paths, account="box@gmail.com", name="dev").name == "dev"
    with pytest.raises(V.VoiceError):
        V.resolve_identity(paths, name="ghost")


def test_no_identities_file_is_not_an_error(tmp_path):
    root = tmp_path / "bare"
    (root / ".navig").mkdir(parents=True)
    (root / ".navig" / "config.yaml").write_text("mailroom: {}\n", encoding="utf-8")
    p = MailroomPaths(root)
    assert V.resolve_identity(p, account="x@y.z") is None
    assert V.available_styles(p)  # the built-ins are still there


def test_broken_yaml_says_so_instead_of_pretending_there_is_no_context(paths):
    (paths.base / "identities.yaml").write_text("identities: [oops\n", encoding="utf-8")
    with pytest.raises(V.VoiceError):
        V.identities(paths)


# ── the prompt contract ─────────────────────────────────────────────────────


def test_the_facts_come_after_the_style_and_the_guardrails(paths):
    style, _ = V.load_style("jobs", paths)
    ident = V.resolve_identity(paths, account="box@gmail.com")
    prompt = V.build_prompt(style, ident, instruction="Say we cannot take the project.")

    i_style = prompt.index(style[:40])
    i_guard = prompt.index("Hard rules")
    i_facts = prompt.index("House Studio")
    i_ask = prompt.index("cannot take the project")
    assert i_style < i_guard < i_facts < i_ask, "the persona must not be the last word"

    # the non-negotiables are present whatever the style
    assert "Never invent a fact" in prompt
    assert "outrank the style" in prompt


def test_a_persona_style_still_carries_the_do_not_impersonate_line(paths):
    for name in ("jobs", "musk"):
        text, _ = V.load_style(name, paths)
        low = text.lower()
        assert "not a disguise" in low
        assert "must never say or imply" in low


def test_empty_identity_fields_are_dropped_rather_than_sent_as_blanks(paths):
    ident = V.resolve_identity(paths, alias="sam")
    brief = ident.brief()
    assert "Answer in: French" in brief
    assert "The organisation:" not in brief  # private has no company


def test_build_prompt_survives_no_identity_at_all(paths):
    style, _ = V.load_style("brief", paths)
    prompt = V.build_prompt(style, None, instruction="")
    assert "Hard rules" in prompt and style[:30] in prompt
