"""MiMo voice engines — official endpoint contracts, pinned from live calls.

Measured live 2026-10-13 against api.xiaomimimo.com with the vault key:

* **TTS** (`mimo-v2.5-tts`): a chat-completions call where the ASSISTANT
  message carries the text to speak; the audio comes back **base64** in
  ``choices[0].message.audio.data`` (200, 21768-byte mp3, voice ``Mia``).
  Free during the promo.
* **ASR** (`mimo-v2.5-asr`): same endpoint with an ``input_audio`` content
  part (mp3/wav ONLY — data URL or base64+format); the transcript is
  ``choices[0].message.content``. Round-tripped a generated mp3 back
  correctly. $0.074 per hour of audio.

Crossing these shapes with the OpenAI ``/audio/speech`` or
``/audio/transcriptions`` conventions 404s — both wrong-shape probes failed
against this host the same day, which is exactly what these tests pin.
"""

from __future__ import annotations

import sys

import pytest

from navig_audio.voice.stt import STT, STTConfig, STTProvider
from navig_audio.voice.tts import TTS, TTSConfig, TTSProvider


class _FakeResp:
    def __init__(self, payload, status=200, body=""):
        self.status = status
        self._payload = payload
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def text(self):
        return self._body

    async def json(self):
        return self._payload


class _FakeSession:
    calls: list = []
    payload: dict = {}
    status: int = 200
    body: str = ""

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, headers=None, json=None, timeout=None, data=None):
        _FakeSession.calls.append({"url": url, "json": json, "headers": headers or {}})
        return _FakeResp(_FakeSession.payload, _FakeSession.status, _FakeSession.body)


@pytest.fixture
def fake_api(monkeypatch):
    _FakeSession.calls = []
    _FakeSession.payload = {}
    _FakeSession.status = 200
    _FakeSession.body = ""
    fake = type("fake_aiohttp", (), {})
    fake.ClientSession = _FakeSession
    fake.ClientTimeout = lambda **kwargs: None
    monkeypatch.setitem(sys.modules, "aiohttp", fake)
    monkeypatch.setattr(
        TTS, "_resolve_api_key", staticmethod(lambda *args, **kwargs: "sk-or-test")
    )
    monkeypatch.setattr(
        STT, "_resolve_api_key", staticmethod(lambda *args, **kwargs: "sk-or-test")
    )
    return _FakeSession


# ── TTS ───────────────────────────────────────────────────────────────────


def test_mimo_is_a_tts_provider_with_the_promo_defaults():
    assert TTSProvider.MIMO.value == "mimo"
    cfg = TTSConfig()
    assert cfg.mimo_model == "mimo-v2.5-tts"
    assert cfg.mimo_voice == "mimo_default"


async def test_mimo_tts_uses_the_chat_shape_and_decodes_audio(fake_api, tmp_path):
    import base64

    fake_api.payload = {
        "choices": [
            {"message": {"audio": {"data": base64.b64encode(b"FAKEMP3AUDIO").decode()}}}
        ]
    }
    tts = TTS(TTSConfig(provider=TTSProvider.MIMO, fallback_providers=[]))

    result = await tts.synthesize(
        "Привет", provider=TTSProvider.MIMO, output_path=tmp_path / "v.mp3"
    )

    assert result.success, result.error
    assert result.audio_path.read_bytes() == b"FAKEMP3AUDIO"
    assert result.voice_compatible is True

    call = fake_api.calls[-1]
    assert call["url"] == "https://api.xiaomimimo.com/v1/chat/completions"
    assert call["json"]["model"] == "mimo-v2.5-tts"
    # The text to speak rides in the ASSISTANT message — the non-obvious half
    # of this contract (the OpenAI shape puts text in "input").
    msgs = call["json"]["messages"]
    assert any(m["role"] == "assistant" and m["content"] == "Привет" for m in msgs)
    assert call["json"]["audio"] == {"format": "mp3", "voice": "mimo_default"}


def _audio_payload(raw: bytes) -> dict:
    import base64

    return {"choices": [{"message": {"audio": {"data": base64.b64encode(raw).decode()}}}]}


def _mimo_only(cache_dir) -> TTS:
    # Hermetic cache dir and no fallback: a MiMo failure must surface as MiMo's error.
    return TTS(TTSConfig(provider=TTSProvider.MIMO, fallback_providers=[], cache_dir=cache_dir))


async def test_mimo_tts_sends_the_chosen_voice_and_an_auth_header(fake_api, tmp_path):
    fake_api.payload = _audio_payload(b"FAKEMP3AUDIO")

    result = await _mimo_only(tmp_path).synthesize(
        "Stream is live", provider=TTSProvider.MIMO, voice="Chloe", output_path=tmp_path / "o.mp3"
    )

    assert result.success, result.error
    assert result.voice == "Chloe"
    call = fake_api.calls[-1]
    assert call["json"]["audio"] == {"format": "mp3", "voice": "Chloe"}
    # The key rides as a Bearer token; only its presence is asserted, never its value.
    assert call["headers"].get("Authorization", "").startswith("Bearer ")
    assert len(call["headers"]["Authorization"]) > len("Bearer ")


async def test_mimo_tts_non_200_is_a_clean_error(fake_api, tmp_path):
    fake_api.status = 401
    fake_api.body = '{"error": "invalid api key"}'
    out = tmp_path / "o.mp3"

    result = await _mimo_only(tmp_path).synthesize(
        "hi", provider=TTSProvider.MIMO, output_path=out
    )

    assert not result.success
    assert "MiMo TTS error 401" in (result.error or "")
    assert not out.exists()


async def test_mimo_tts_empty_audio_is_an_error_and_is_not_cached(fake_api, tmp_path):
    fake_api.payload = {"choices": [{"message": {"audio": {"data": ""}}}]}

    result = await _mimo_only(tmp_path).synthesize("hi", provider=TTSProvider.MIMO)

    assert not result.success
    assert "no audio data" in (result.error or "")
    assert not list(tmp_path.glob("*.mp3"))


async def test_mimo_tts_without_a_key_fails_without_calling_the_api(
    fake_api, tmp_path, monkeypatch
):
    monkeypatch.setattr(TTS, "_resolve_api_key", staticmethod(lambda *a, **k: None))

    result = await _mimo_only(tmp_path).synthesize("hi", provider=TTSProvider.MIMO)

    assert not result.success
    assert "MIMO_API_KEY" in (result.error or "")
    assert fake_api.calls == []


async def test_a_cache_hit_still_writes_the_pinned_output_file(fake_api, tmp_path):
    # `navig voice speak TEXT --provider mimo -o FILE --no-play` is the contract the
    # stream overlay relies on: FILE must exist even when the line was cached earlier.
    fake_api.payload = _audio_payload(b"FIRSTCALLAUDIO")
    cache = tmp_path / "cache"
    await _mimo_only(cache).synthesize("New follower", provider=TTSProvider.MIMO)
    assert len(fake_api.calls) == 1

    out = tmp_path / "say" / "line.mp3"
    result = await _mimo_only(cache).synthesize(
        "New follower", provider=TTSProvider.MIMO, output_path=out
    )

    assert result.success, result.error
    assert len(fake_api.calls) == 1  # served from cache, no second API call
    assert result.audio_path == out
    assert out.read_bytes() == b"FIRSTCALLAUDIO"


async def test_mimo_lists_its_built_in_voices():
    ids = [v["id"] for v in await TTS(TTSConfig()).list_voices(TTSProvider.MIMO)]
    assert ids[0] == "mimo_default"
    assert {"Mia", "Chloe", "Milo", "Dean", "冰糖", "茉莉", "苏打", "白桦"} <= set(ids)


def test_speak_help_names_mimo():
    from typer.testing import CliRunner

    from navig_audio.commands.voice import voice_app

    out = CliRunner().invoke(voice_app, ["speak", "--help"]).output
    assert "mimo" in out


# ── ASR ───────────────────────────────────────────────────────────────────


def test_mimo_is_an_stt_provider_with_the_asr_default():
    assert STTProvider.MIMO.value == "mimo"
    assert STTConfig().mimo_model == "mimo-v2.5-asr"


async def test_mimo_asr_posts_input_audio_and_reads_the_transcript(fake_api, tmp_path):
    fake_api.payload = {
        "choices": [{"message": {"content": "Hello from Navig."}}]
    }
    clip = tmp_path / "clip.mp3"
    clip.write_bytes(b"ID3FAKEMP3")
    stt = STT(config=STTConfig(provider=STTProvider.MIMO, fallback_providers=[]))

    result = await stt.transcribe(clip, provider=STTProvider.MIMO)

    assert result.success, result.error
    assert result.text == "Hello from Navig."
    assert result.provider == STTProvider.MIMO

    call = fake_api.calls[-1]
    assert call["json"]["model"] == "mimo-v2.5-asr"
    part = call["json"]["messages"][0]["content"][0]
    assert part["type"] == "input_audio"
    assert part["input_audio"]["format"] == "mp3"
    assert part["input_audio"]["data"]  # base64 payload present


async def test_mimo_asr_names_a_missing_ffmpeg_for_ogg_input(fake_api, tmp_path, monkeypatch):
    """Telegram voice is .ogg — the mp3/wav-only API needs ffmpeg to accept it.
    A missing binary must be a NAMED, fixable error, not a raw stack."""
    import subprocess

    clip = tmp_path / "voice.ogg"
    clip.write_bytes(b"OggSFAKE")
    monkeypatch.setattr(subprocess, "run", _raise_file_not_found)
    stt = STT(config=STTConfig(provider=STTProvider.MIMO, fallback_providers=[]))

    result = await stt.transcribe(clip, provider=STTProvider.MIMO)

    assert not result.success
    assert "ffmpeg" in (result.error or "")


def _raise_file_not_found(*args, **kwargs):
    raise FileNotFoundError("ffmpeg")

async def test_mimo_tts_carries_the_callers_style_and_the_spoken_lexicon(fake_api, tmp_path):
    fake_api.payload = _audio_payload(b"FAKEMP3AUDIO")

    result = await _mimo_only(tmp_path).synthesize(
        "SCHEMA deployed.",
        provider=TTSProvider.MIMO,
        voice="Dean",
        output_path=tmp_path / "s.mp3",
        lang="en",
        style="a warm, wry city pigeon",
    )

    assert result.success, result.error
    msgs = fake_api.calls[-1]["json"]["messages"]
    assert {"role": "user", "content": "a warm, wry city pigeon"} in msgs
    # The engine hears the pronunciation, never the capitals it used to spell out.
    assert {"role": "assistant", "content": "Skeema deployed."} in msgs


async def test_a_new_style_is_a_new_clip_not_a_cache_hit(fake_api, tmp_path):
    tts = _mimo_only(tmp_path)
    plain = tts._get_cache_key("hi", TTSProvider.MIMO, "Dean")
    styled = tts._get_cache_key("hi", TTSProvider.MIMO, "Dean", "a pigeon")
    assert plain != styled
    # No style keeps the historic key, so clips cached before styles still hit.
    assert plain == tts._get_cache_key("hi", TTSProvider.MIMO, "Dean", None)
