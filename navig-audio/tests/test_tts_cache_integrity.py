"""A poisoned TTS cache entry — a 0-byte file from a 200-but-empty API response or a crash
mid-write — must never be served as a valid hit (silence forever, never re-synthesized), and
a 'successful' synth that produced empty audio must never be cached.
"""
from __future__ import annotations

import asyncio

from navig_audio.voice.tts import TTS, TTSConfig, TTSProvider, TTSResult


def _engine(tmp_path):
    return TTS(TTSConfig(cache_enabled=True, cache_dir=tmp_path, provider=TTSProvider.OPENAI))


def test_get_cached_rejects_and_heals_empty_file(tmp_path):
    tts = _engine(tmp_path)
    key = "deadbeefdeadbeef"
    empty = tmp_path / f"{key}.mp3"
    empty.write_bytes(b"")  # 0-byte poison

    assert tts._get_cached(key) is None   # not served as a hit
    assert not empty.exists()             # self-healed: deleted so the next call re-synthesizes


def test_get_cached_returns_nonempty_file(tmp_path):
    tts = _engine(tmp_path)
    key = "cafebabecafebabe"
    good = tmp_path / f"{key}.mp3"
    good.write_bytes(b"REALAUDIO")
    assert tts._get_cached(key) == good


def test_get_cached_drops_stale_inmemory_empty(tmp_path):
    tts = _engine(tmp_path)
    key = "0011223344556677"
    empty = tmp_path / f"{key}.mp3"
    empty.write_bytes(b"")
    tts._cache[key] = empty               # a stale in-memory entry pointing at an empty file
    assert tts._get_cached(key) is None
    assert key not in tts._cache          # dropped


def test_synthesize_does_not_cache_empty_audio(tmp_path):
    tts = _engine(tmp_path)

    async def _empty_provider(text, provider, voice, output_path, **kwargs):
        if output_path is None:
            output_path = tts._get_temp_path("fake", ".mp3")
        output_path.write_bytes(b"")  # provider claims success but produced nothing
        return TTSResult(success=True, audio_path=output_path, provider=provider, voice=voice)

    tts._synthesize_with_provider = _empty_provider  # type: ignore[method-assign]
    r = asyncio.run(tts.synthesize("hello world"))

    assert r.success is False                       # empty audio is not a real success
    assert list(tmp_path.glob("*.mp3")) == []       # no 0-byte file left to poison the cache
