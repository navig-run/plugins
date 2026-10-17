"""Voice profiles, the routing policy, the usage ledger, and the sound-aware cache key."""

from __future__ import annotations

import json

import pytest

from navig_audio.voice import catalog as cat
from navig_audio.voice import usage
from navig_audio.voice.profiles import apply_pronounce, parse_pronounce, plan_from_params
from navig_audio.voice.tts import TTS, TTSConfig, TTSProvider


@pytest.fixture
def keys(monkeypatch):
    found: dict[str, str] = {}
    monkeypatch.setattr(cat, "_installed", lambda module: True)
    monkeypatch.setattr(TTS, "_resolve_api_key", staticmethod(lambda provider, *env: found.get(provider)))
    return found


# ── profiles ──────────────────────────────────────────────────────────────────


def test_edge_profile_maps_pitch_and_speed():
    plan = plan_from_params({"provider": "edge", "voice": "ru-RU-DmitryNeural", "lang": "ru", "speed": 0.9, "pitch_hz": -6})
    assert plan.provider is TTSProvider.EDGE
    assert plan.config == {"speed": 0.9, "pitch": "-6Hz"}
    assert plan.lang == "ru" and plan.voice == "ru-RU-DmitryNeural" and plan.ignored == []


def test_elevenlabs_profile_maps_model_and_voice_settings_and_reports_the_rest():
    plan = plan_from_params({
        "provider": "elevenlabs", "model": "eleven_flash_v2_5", "stability": 0.6, "similarity": 0.8,
        "pitch_hz": 5, "speed": 1.2, "style": "warm",
    })
    assert plan.config == {"elevenlabs_model": "eleven_flash_v2_5", "elevenlabs_stability": 0.6, "elevenlabs_similarity": 0.8}
    assert sorted(plan.ignored) == ["pitch_hz", "speed", "style"]
    assert plan.speed == 1.2  # still the reader's playback rate, even if the engine cannot


def test_auto_profile_keeps_fields_for_the_routed_engine():
    plan = plan_from_params({"provider": "auto", "lang": "auto", "speed": 0.9})
    assert plan.provider is None and plan.lang is None and plan.ignored == []


def test_every_engine_provider_is_a_valid_profile_provider():
    from navig.presets.model import SPEECH_PROVIDERS

    assert set(SPEECH_PROVIDERS) == {"auto", *(p.value for p in TTSProvider)}


def test_pronunciation_rules():
    pairs = parse_pronounce(["SQL=sequel", "NAVIG OS = навиг ОС", "NAVIG=навиг", "bad", "=x", "C++=си плюс плюс"])
    assert ("bad", "") not in pairs and len(pairs) == 4
    text = "NAVIG OS uses SQL; navig is not navigator. C++ too."
    out = apply_pronounce(text, pairs)
    assert out == "навиг ОС uses sequel; навиг is not navigator. си плюс плюс too."
    # Cyrillic word boundaries are respected
    assert apply_pronounce("Схема схемами", [("схема", "skeema")]) == "skeema схемами"


# ── policy ────────────────────────────────────────────────────────────────────


def test_route_under_each_policy(keys):
    assert cat.route(None, "auto").provider is TTSProvider.EDGE
    assert cat.route(None, "free").provider is TTSProvider.EDGE
    with pytest.raises(cat.RoutingError, match="Offline"):
        cat.route(None, "offline")
    r = cat.route(None, "premium")
    assert r.provider is TTSProvider.EDGE and "no paid voice" in r.note
    keys["elevenlabs"] = "k"
    r = cat.route(None, "premium")
    assert r.provider is TTSProvider.ELEVENLABS and r.paid
    assert cat.route(None, "auto").provider is TTSProvider.EDGE  # auto never picks paid itself
    assert cat.route(TTSProvider.ELEVENLABS, "auto").paid  # naming it is the opt-in
    with pytest.raises(cat.RoutingError, match="Free mode"):
        cat.route(TTSProvider.ELEVENLABS, "free")
    with pytest.raises(cat.RoutingError, match="online service"):
        cat.route(TTSProvider.EDGE, "offline")


def test_prices_are_recorded_with_their_source():
    for p in TTSProvider:
        row = cat.provider_status(p)["price"]
        assert row["checked"] == cat.PRICES_CHECKED and row["source"]
    assert cat.estimate_usd(TTSProvider.ELEVENLABS, 1000) == pytest.approx(0.08)
    assert cat.estimate_usd(TTSProvider.OPENROUTER, 1000) is None


# ── ledger ────────────────────────────────────────────────────────────────────


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "voice" / "usage.json"
    monkeypatch.setattr(usage, "ledger_path", lambda: path)
    return path


def test_ledger_records_per_provider_and_day(ledger):
    usage.record("elevenlabs", 100, usd=0.008)
    usage.record("elevenlabs", 50, usd=0.004)
    usage.record("openai", 10)
    usage.record("openai", 0)  # nothing spent, nothing recorded
    assert usage.spent_today() == 160
    rows = usage.day_rows()
    assert rows["elevenlabs"] == {"chars": 150, "requests": 2, "usd": 0.012}
    assert usage.recent(7)[0]["requests"] == 3


def test_ledger_keeps_a_bounded_history(ledger):
    for d in range(70):
        usage.record("openai", 1, day=f"2026-01-{d:02d}" if d < 32 else f"2026-02-{d - 32:02d}")
    days = json.loads(ledger.read_text(encoding="utf-8"))["days"]
    assert len(days) == usage.KEEP_DAYS


def test_an_unreadable_ledger_is_kept_aside_never_overwritten(ledger):
    ledger.parent.mkdir(parents=True)
    ledger.write_text("{not json", encoding="utf-8")
    assert usage.spent_today() == 0  # the read path degrades
    usage.record("openai", 5)
    assert ledger.with_suffix(".json.bad").read_text(encoding="utf-8") == "{not json"
    assert usage.spent_today() == 5


# ── cache key ─────────────────────────────────────────────────────────────────


def test_cache_key_changes_with_the_sound_but_old_keys_stay_valid():
    base = TTS(TTSConfig())._get_cache_key("hello", TTSProvider.EDGE, "en-US-JennyNeural")
    import hashlib

    # The pre-change key for default settings — existing cached clips keep hitting.
    legacy = hashlib.sha256(b"edge:en-US-JennyNeural:hello").hexdigest()[:16]
    assert base == legacy
    lower = TTS(TTSConfig(pitch="-6Hz"))._get_cache_key("hello", TTSProvider.EDGE, "en-US-JennyNeural")
    slower = TTS(TTSConfig(speed=0.9))._get_cache_key("hello", TTSProvider.EDGE, "en-US-JennyNeural")
    assert len({base, lower, slower}) == 3


def test_cached_clip_sees_what_synthesize_would_serve(tmp_path):
    tts = TTS(TTSConfig(cache_dir=tmp_path))
    assert tts.cached_clip("Hello there.", TTSProvider.EDGE, "en-US-JennyNeural") is None
    key = tts._get_cache_key(tts._prepare_text("Hello there."), TTSProvider.EDGE, "en-US-JennyNeural")
    (tmp_path / f"{key}.mp3").write_bytes(b"ID3x")
    assert tts.cached_clip("Hello there.", TTSProvider.EDGE, "en-US-JennyNeural") == tmp_path / f"{key}.mp3"
