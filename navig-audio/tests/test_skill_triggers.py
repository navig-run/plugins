"""The audio-generation skill must activate for the languages the owner writes in.

Skill activation is a case-insensitive substring match over ``activation_keywords``
(``navig.agent.skills_context._compute_activation_score``). The list was English only,
so ``сгенерируй хоррор музыку`` scored 0: the skill — which now carries the
``gen --json`` → ``chat_send_file`` recipe — never reached the model, and a Russian
Telegram request for music got none of the delivery wiring.

Pinned against the real SKILL.md through the real parser and scorer, so a keyword
that is YAML-valid but never matches (or a file the parser cannot read as UTF-8)
fails here rather than in a chat.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from navig.agent.skills_context import _compute_activation_score, _parse_skill_file

SKILL = (
    Path(__file__).resolve().parents[1]
    / "navig_audio"
    / "skills"
    / "audio-generation"
    / "SKILL.md"
)


@pytest.fixture(scope="module")
def skill():
    parsed = _parse_skill_file(SKILL, "test")
    assert parsed is not None, "the audio-generation SKILL.md must parse"
    return parsed


def _score(skill, message: str) -> int:
    return _compute_activation_score(skill, [], message.lower())


@pytest.mark.parametrize(
    "message",
    [
        "make me some horror music",  # the English path must keep working
        "сгенерируй тёмную хоррор музыку",
        "Нужна музыка для стрима",
        "сделай джингл для канала",
        "запиши озвучку этого текста",
        "нужен звуковой эффект двери",
        "génère une musique d'ambiance",
        "fais un bruitage de pas",
        "une voix off pour la vidéo",
    ],
)
def test_the_skill_activates_for_the_request(skill, message):
    assert _score(skill, message) > 0, f"{message!r} would never load the audio skill"


@pytest.mark.parametrize(
    "message",
    [
        "какая сегодня погода",
        "покажи статус git",
        "what's the weather today",
        "restart the service",
    ],
)
def test_the_skill_stays_quiet_for_unrelated_requests(skill, message):
    """Wider triggers must not turn the audio skill into noise on every turn."""
    assert _score(skill, message) == skill.priority, (
        f"{message!r} activated the audio skill beyond its base priority"
    )


def test_the_frontmatter_declares_a_pack_per_language(skill):
    """English stays the plain list; Russian and French live in their own packs
    (`activation_keywords_ru` / `_fr`) — the shape the skill loader merges."""
    assert "music" in [k.lower() for k in skill.activation_keywords], "English triggers survive"
    assert not any(
        ord(ch) > 0x400 for k in skill.activation_keywords for ch in k
    ), "foreign words belong in a per-language pack, not the English list"
    assert any("музык" in k for k in skill.localized_keywords["ru"]), "no Russian pack"
    assert any("musique" in k for k in skill.localized_keywords["fr"]), "no French pack"


def test_audio_enhance_skill_parses_and_activates():
    path = SKILL.parents[1] / "audio-enhance" / "SKILL.md"
    parsed = _parse_skill_file(path, "test")
    assert parsed is not None, "the audio-enhance SKILL.md must parse"
    for msg in ("can you clean up this recording", "remove the hum from take.wav",
                "убери шум из записи", "améliore ce son"):
        assert _compute_activation_score(parsed, [], msg.lower()) > 0, msg
