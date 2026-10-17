"""The bedtime-reader skill must load for "read this to me" — and ONLY for that.

Skill activation is a lexical match (see ``navig.agent.skills_context``). Two failure modes
matter and each is pinned:

* **recall** — the owner writes Russian and French: "почитай мне", "lis-moi ça à voix haute"
  must load the skill, or the agent never learns the cost rules and the ``.txt`` route;
* **precision** — "прочитай файл config.yaml" asks the agent to READ A FILE, and "озвучь этот
  текст" asks for a voiceover FILE (audio-generation's job). Loading the reader there would
  put an 8 KB skill into the context of every file-reading turn.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from navig.agent.skills_context import _compute_activation_score, _parse_skill_file

SKILLS = Path(__file__).resolve().parents[1] / "navig_audio" / "skills"


@pytest.fixture(scope="module")
def reader_skill():
    skill = _parse_skill_file(SKILLS / "bedtime-reader" / "SKILL.md", "test")
    assert skill is not None, "the bedtime-reader SKILL.md must parse"
    return skill


@pytest.fixture(scope="module")
def generation_skill():
    return _parse_skill_file(SKILLS / "audio-generation" / "SKILL.md", "test")


def _score(skill, message: str) -> int:
    return _compute_activation_score(skill, [], message.lower()) - skill.priority


@pytest.mark.parametrize(
    "message",
    [
        "read this to me",
        "Can you read me a story?",
        "I want an audiobook of this chapter",
        "read it like a book please",
        "почитай мне эту статью",
        "прочитай мне вслух главу",
        "прочитай вслух этот текст",
        "хочу послушать как книгу",
        "сделай мне аудиокнигу из этого",
        "lis-moi cette histoire",
        "lis moi ça à voix haute",
        "je veux un livre audio de ce chapitre",
        "lis-le comme un livre",
    ],
)
def test_it_loads_for_a_request_to_be_read_to(reader_skill, message):
    assert _score(reader_skill, message) > 0, f"{message!r} would never load the bedtime reader"


@pytest.mark.parametrize(
    "message",
    [
        "прочитай файл config.yaml и скажи что в нём",  # read a FILE for analysis
        "почитай про docker и расскажи",  # "go read about X"
        "lis le fichier de configuration",
        "read the README and summarise it",
        "please read the logs",
        "озвучь этот текст приятным голосом",  # a voiceover FILE: audio-generation
        "Сгенерируй тёмную хоррор музыку",
        "génère une musique d'ambiance sombre",
        "какая сегодня погода",
        "raconte-moi une blague",
        "напомни принять таблетки перед сном",  # sleep is not reading
    ],
)
def test_it_stays_out_of_the_way_of_other_requests(reader_skill, message):
    assert _score(reader_skill, message) == 0, f"{message!r} wrongly loads the bedtime reader"


@pytest.mark.parametrize(
    ("message", "winner"),
    [
        ("озвучь этот текст приятным голосом", "audio-generation"),
        ("сделай джингл для моего канала", "audio-generation"),
        ("почитай мне эту статью", "bedtime-reader"),
        ("lis-moi cette histoire à voix haute", "bedtime-reader"),
    ],
)
def test_the_two_audio_skills_do_not_steal_each_others_requests(
    reader_skill, generation_skill, message, winner
):
    reader, generation = _score(reader_skill, message), _score(generation_skill, message)
    top = "bedtime-reader" if reader > generation else "audio-generation"
    assert top == winner, f"{message!r}: reader={reader} generation={generation}"


def test_the_skill_carries_a_pack_per_language_and_the_english_list(reader_skill):
    assert reader_skill.activation_keywords and "bedtime" in reader_skill.activation_keywords
    assert {"ru", "fr"} <= set(reader_skill.localized_keywords)


def test_the_skill_tells_the_agent_the_cost_rules(reader_skill):
    """The body is the point: without these three lines the agent re-sends a chapter to say
    'yes', summarises the text it was asked to read, or pastes 20k characters into a tool."""
    # Normalise whitespace: the body is wrapped Markdown, and where a line breaks must not
    # decide whether a rule is "present".
    body = " ".join(reader_skill.content.split())
    assert "exactly as given" in body and "Never shorten" in body
    assert "confirmed=true" in body and "no text" in body
    assert "`.txt` or `.md` file with the caption `/read`" in body
