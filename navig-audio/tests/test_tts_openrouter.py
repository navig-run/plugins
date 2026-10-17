"""OpenRouter Fish Audio TTS — endpoint, payload shape, and voice honesty.

Measured live 2026-10-12 against the real endpoint:
  * `fish-audio/s2.1-pro` answers (77 KB mp3) — the default here.
  * `fish-audio/s2.1-pro-free` 404s "No endpoints found" — a picker entry for
    it would be a dead choice, so it must NOT be the default.
  * The response is a RAW bytestream, not JSON — reading it as JSON (or vice
    versa) is the classic OpenAI-compat TTS mixup.
  * `voice` is optional: omitted when unset so Fish Audio uses its model
    default instead of rejecting an OpenAI voice id like "en-US-JennyNeural".
"""

from __future__ import annotations

import sys

import pytest

from navig_audio.voice.tts import TTS, TTSConfig, TTSProvider


class _FakeResp:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def text(self):  # pragma: no cover - only on error paths
        return ""

    async def read(self):
        return b"FAKEMP3BYTES"


class _FakeSession:
    calls: list = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, headers=None, json=None, timeout=None):
        _FakeSession.calls.append({"url": url, "headers": headers, "json": json})
        return _FakeResp()


@pytest.fixture
def fake_openrouter(monkeypatch):
    _FakeSession.calls = []
    fake = type("fake_aiohttp", (), {})
    fake.ClientSession = _FakeSession
    fake.ClientTimeout = lambda **kwargs: None
    monkeypatch.setitem(sys.modules, "aiohttp", fake)
    monkeypatch.setattr(
        TTS, "_resolve_api_key", staticmethod(lambda *args, **kwargs: "sk-or-test")
    )
    return _FakeSession


def test_openrouter_is_a_provider_enum_member():
    assert TTSProvider.OPENROUTER.value == "openrouter"
    assert TTSConfig().openrouter_model == "fish-audio/s2.1-pro"


async def test_synthesize_posts_the_openai_shaped_payload(fake_openrouter, tmp_path):
    tts = TTS(TTSConfig(provider=TTSProvider.OPENROUTER, fallback_providers=[]))
    out = tmp_path / "voice.mp3"

    result = await tts.synthesize(
        "Привет, мир", provider=TTSProvider.OPENROUTER, output_path=out
    )

    assert result.success, result.error
    assert result.audio_path == out
    assert out.read_bytes() == b"FAKEMP3BYTES"  # raw bytestream, not JSON
    assert result.voice_compatible is True

    call = fake_openrouter.calls[-1]
    assert call["url"] == "https://openrouter.ai/api/v1/audio/speech"
    assert call["headers"]["Authorization"] == "Bearer sk-or-test"
    assert call["json"]["model"] == "fish-audio/s2.1-pro"
    assert call["json"]["input"] == "Привет, мир"
    assert call["json"]["response_format"] == "mp3"
    # voice UNSET → omitted entirely (the model's default voice), never a
    # leaked OpenAI/Edge voice id from another provider's picker.
    assert "voice" not in call["json"]


async def test_an_explicit_fish_voice_is_forwarded(fake_openrouter, tmp_path):
    tts = TTS(TTSConfig(provider=TTSProvider.OPENROUTER, fallback_providers=[]))

    result = await tts.synthesize(
        "hello", provider=TTSProvider.OPENROUTER,
        output_path=tmp_path / "v.mp3", voice="fish-voice-42",
    )

    assert result.success, result.error
    assert fake_openrouter.calls[-1]["json"].get("voice") == "fish-voice-42"