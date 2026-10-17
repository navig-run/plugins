"""Dictation routing (voice policy) and the deterministic text modes."""

from __future__ import annotations

import pytest

from navig_audio.voice import dictation as dc
from navig_audio.voice.stt import STTProvider


@pytest.fixture
def env(monkeypatch):
    keys: dict[str, str] = {}
    state = {"local": True}
    from navig_audio.voice.tts import TTS

    monkeypatch.setattr(TTS, "_resolve_api_key", staticmethod(lambda provider, *e: keys.get(provider)))
    monkeypatch.setattr(dc, "local_available", lambda: state["local"])
    return {"keys": keys, "state": state}


def test_auto_free_offline_stay_on_device(env):
    for policy in ("auto", "free", "offline"):
        r = dc.route_stt(policy)
        assert r.provider is STTProvider.WHISPER_LOCAL and r.on_device


def test_cloud_only_when_named_and_keyed_and_never_in_free_or_offline(env):
    with pytest.raises(dc.DictationRoutingError, match="no API key"):
        dc.route_stt("auto", STTProvider.ELEVENLABS)
    env["keys"]["elevenlabs"] = "k"
    assert dc.route_stt("auto", STTProvider.ELEVENLABS).provider is STTProvider.ELEVENLABS
    assert dc.route_stt("auto").provider is STTProvider.WHISPER_LOCAL  # never picked by itself
    with pytest.raises(dc.DictationRoutingError, match="Free mode"):
        dc.route_stt("free", STTProvider.ELEVENLABS)
    with pytest.raises(dc.DictationRoutingError, match="Offline mode"):
        dc.route_stt("offline", STTProvider.ELEVENLABS)


def test_premium_prefers_a_keyed_cloud_engine(env):
    r = dc.route_stt("premium")
    assert r.provider is STTProvider.WHISPER_LOCAL and "no cloud" in r.note
    env["keys"]["openai"] = "k"
    assert dc.route_stt("premium").provider is STTProvider.WHISPER_API


def test_no_engine_at_all_is_a_clear_error(env):
    env["state"]["local"] = False
    with pytest.raises(dc.DictationRoutingError, match="faster-whisper"):
        dc.route_stt("offline")


def test_make_stt_pins_the_engine_without_a_fallback_chain():
    stt = dc.make_stt(dc.SttRoute(STTProvider.WHISPER_LOCAL, True), local_model="small")
    assert stt.config.provider is STTProvider.WHISPER_LOCAL
    assert stt.config.fallback_providers == []
    assert stt.config.whisper_local_model == "small"
    assert dc.make_stt(dc.SttRoute(STTProvider.WHISPER_LOCAL, True), local_model="huge").config.whisper_local_model == "base"


# ── modes ─────────────────────────────────────────────────────────────────────


def test_verbatim_never_rewrites():
    raw = "  um so this is   what I said , uh , really  "
    assert dc.shape(raw, "verbatim", "en") == "um so this is   what I said , uh , really"


def test_clean_fixes_spacing_and_capitals_only():
    assert dc.shape("hello world , this is it.and more", "clean", "en") == "Hello world, this is it.and more"
    assert dc.shape("first one. second one? third", "clean", "en") == "First one. Second one? Third"
    # French high punctuation takes a space before it
    assert dc.shape("bonjour ! ça va ? oui", "clean", "fr") == "Bonjour ! Ça va ? Oui"
    assert dc.shape("привет. как дела", "clean", "ru") == "Привет. Как дела"


def test_no_fillers_removes_only_fillers():
    assert dc.shape("um, so uh I think, erm, yes", "no_fillers", "en") == "So I think, yes"
    assert dc.shape("euh je pense, heu, que oui", "no_fillers", "fr") == "Je pense, que oui"
    assert dc.shape("ээ я думаю, эм, что да", "no_fillers", "ru") == "Я думаю, что да"
    assert dc.shape("a test , uh , really", "no_fillers", "en") == "A test, really"
    assert dc.shape(", um, hello", "no_fillers", "en") == "Hello"
    # words that merely contain a filler survive
    assert dc.shape("the umbrella is here", "no_fillers", "en") == "The umbrella is here"


def test_whisper_models_are_loaded_once(monkeypatch):
    from navig_audio.voice import stt

    loads = []

    def factory(name, device, compute_type):
        loads.append((name, device))
        return object()

    monkeypatch.setattr(stt, "_WHISPER_MODELS", {})
    a, _ = stt._cached_whisper(factory, "base", "cpu")
    b, _ = stt._cached_whisper(factory, "base", "cpu")
    assert a is b and loads == [("base", "cpu")]
    stt._cached_whisper(factory, "small", "cpu")
    stt._cached_whisper(factory, "medium", "cpu")  # third model evicts the oldest
    assert ("base", "cpu") not in stt._WHISPER_MODELS and len(stt._WHISPER_MODELS) == 2
