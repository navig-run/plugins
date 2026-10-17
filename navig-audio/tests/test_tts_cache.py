"""The persistent TTS disk cache must actually HIT across engine instances.

The disk read looks for ``cache_dir/<key>.mp3``, but the write used to land at a
per-call ``cache_dir/<provider>_<timestamp>.mp3`` — a name the read never found. So a
repeated phrase was re-synthesized (re-billing the paid TTS API) on every new process,
and each call dropped another file into the cache dir (unbounded growth). The engine's
in-memory cache masked it within a single process.
"""

from __future__ import annotations

import asyncio

from navig_audio.voice.tts import TTS, TTSConfig, TTSProvider, TTSResult


def _synth(engine: TTS, counter: dict, text: str = "hello world") -> TTSResult:
    """Drive one synthesize() with the provider dispatcher stubbed to write a fake
    audio file to whatever output path the engine chose, counting real 'API' calls."""

    async def _fake_provider(text, provider, voice, output_path, **kwargs):
        counter["n"] += 1
        if output_path is None:  # mirror the real providers' fallback (tts.py:371-372)
            output_path = engine._get_temp_path("fake", ".mp3")
        output_path.write_bytes(b"FAKEAUDIO")
        return TTSResult(success=True, audio_path=output_path, provider=provider, voice=voice)

    engine._synthesize_with_provider = _fake_provider  # type: ignore[method-assign]
    return asyncio.run(engine.synthesize(text))


def test_disk_cache_hits_across_instances_and_stays_bounded(tmp_path):
    cfg = TTSConfig(cache_enabled=True, cache_dir=tmp_path, provider=TTSProvider.OPENAI)

    # First engine: a real synth writes exactly ONE deterministic cache file.
    c1 = {"n": 0}
    r1 = _synth(TTS(cfg), c1)
    assert r1.success and c1["n"] == 1
    mp3s = list(tmp_path.glob("*.mp3"))
    assert len(mp3s) == 1  # one <key>.mp3, not a timestamped per-call file
    assert r1.audio_path == mp3s[0]

    # A FRESH engine == a new process (empty in-memory cache), SAME cache_dir.
    c2 = {"n": 0}
    r2 = _synth(TTS(cfg), c2)
    assert r2.success
    assert c2["n"] == 0  # ← disk cache HIT: the provider (paid API) was NOT called
    assert r2.audio_path == mp3s[0]  # served the cached file

    # Repeated synths never accumulate more files (no unbounded growth).
    _synth(TTS(cfg), {"n": 0})
    assert len(list(tmp_path.glob("*.mp3"))) == 1


def test_cache_disabled_uses_a_throwaway_temp_file(tmp_path):
    # With caching off, nothing is written into the (unused) cache dir; the audio lands
    # in a system temp file instead.
    cfg = TTSConfig(cache_enabled=False, cache_dir=tmp_path, provider=TTSProvider.OPENAI)
    r = _synth(TTS(cfg), {"n": 0})
    assert r.success
    assert r.audio_path is not None and r.audio_path.exists()
    assert not list(tmp_path.glob("*.mp3"))  # cache dir untouched when disabled
    r.audio_path.unlink(missing_ok=True)
