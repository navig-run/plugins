"""The speech capability catalog: honest availability, cost class, and Auto's free-only rule."""

from __future__ import annotations

import pytest

from navig_audio.voice import catalog as cat
from navig_audio.voice.tts import TTS, TTSProvider


@pytest.fixture
def keys(monkeypatch):
    found: dict[str, str] = {}
    monkeypatch.setattr(cat, "_installed", lambda module: True)
    monkeypatch.setattr(TTS, "_resolve_api_key", staticmethod(lambda provider, *env: found.get(provider)))
    return found


def test_every_engine_provider_has_a_catalog_row():
    # A provider added to the engine without a catalog row would be invisible to every
    # client (and pick_provider would KeyError on it).
    assert set(cat._SPECS) == set(TTSProvider)


def test_auto_only_lists_free_engines():
    for prov in cat.AUTO_ORDER:
        assert cat._SPECS[prov].cost_class == cat.FREE


def test_auto_never_picks_a_paid_engine_even_when_keyed(keys):
    keys.update({"elevenlabs": "k", "openai": "k", "mimo": "k"})
    assert cat.pick_provider(None) is TTSProvider.EDGE


def test_named_provider_needs_its_key(keys):
    with pytest.raises(cat.RoutingError, match="API key"):
        cat.pick_provider(TTSProvider.ELEVENLABS)
    keys["elevenlabs"] = "k"
    assert cat.pick_provider(TTSProvider.ELEVENLABS) is TTSProvider.ELEVENLABS


def test_parse_provider():
    assert cat.parse_provider(None) is None
    assert cat.parse_provider("AUTO") is None
    assert cat.parse_provider(" edge ") is TTSProvider.EDGE
    with pytest.raises(cat.RoutingError, match="unknown provider"):
        cat.parse_provider("piper")


def test_core_languages_have_edge_voices():
    for lang in cat.CORE_LANGUAGES:
        assert cat.default_voice(TTSProvider.EDGE, lang).startswith(
            {"fr": "fr-FR", "ru": "ru-RU", "en": "en-"}[lang]
        )


def test_edge_voice_shape_check():
    assert cat.known_voice(TTSProvider.EDGE, "de-DE-KatjaNeural")
    assert not cat.known_voice(TTSProvider.EDGE, "Katja")
    assert not cat.known_voice(TTSProvider.EDGE, "")
    assert cat.known_voice(TTSProvider.ELEVENLABS, "anyClonedVoiceId")
