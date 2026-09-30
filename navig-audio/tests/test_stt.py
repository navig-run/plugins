"""Speech-to-text must never pin a language nobody chose.

`STTConfig.language` shipped as a hard-coded ``"en"`` with ``detect_language``
off, and no caller passed a language — so every transcription in navig (the
TikTok Transcript button, the catalog's video→text, voice notes) was *told* the
audio was English. Russian speech came back decoded as English, and anything
built on that transcript inherited the wrong language too.

Detection is driven by the language being **absent**, not by flipping
``detect_language`` on, so the empty default is the whole mechanism.
"""

from __future__ import annotations


def test_stt_config_does_not_ship_a_language_default():
    from navig_audio.voice.stt import STTConfig

    assert STTConfig().language == "", (
        "a default language silently mistranscribes every non-English recording"
    )


def test_detection_is_not_achieved_by_defaulting_the_flag_on():
    """The flag stays off by default; an absent language is what asks for detect.

    Pinning both would make an explicitly configured language unreachable.
    """
    from navig_audio.voice.stt import STTConfig

    assert STTConfig().detect_language is False


# ── a pinned language arrives as a NAME; every provider wants a CODE ──────────
#
# The bug above was "nothing pinned inherits English". Its mirror image is
# "something pinned is refused": `user.language` is written by a human, so it
# holds "Russian", and Deepgram / the Whisper API / local Whisper all want an ISO
# code. Measured on a real clip via faster-whisper: `None` and `'ru'` transcribe,
# `'Russian'` fails with "'Russian' is not a valid language code" — so a pinned
# language was strictly WORSE than pinning nothing, which is the opposite of what
# a preference is for.
#
# This is the second of navig's two STT entry points (the video-transcript path;
# `navig.agent.voice_input` is the other) and both convert at their own seam.


async def _lang_reaching_the_provider(monkeypatch, tmp_path, pinned):
    """Run STT.transcribe and report the language string the provider was handed."""
    from navig_audio.voice.stt import STT, STTConfig, STTProvider, STTResult

    clip = tmp_path / "a.wav"
    clip.write_bytes(b"\x00" * 64)
    seen: list = []

    stt = STT(STTConfig(provider=STTProvider.DEEPGRAM))

    async def _spy(audio_path, provider, language, **kwargs):
        seen.append(language)
        return STTResult(success=True, text="ok")

    monkeypatch.setattr(stt, "_transcribe_with_provider", _spy)
    await stt.transcribe(clip, language=pinned)
    return seen


async def test_a_language_name_reaches_the_provider_as_a_code(monkeypatch, tmp_path):
    assert await _lang_reaching_the_provider(monkeypatch, tmp_path, "Russian") == ["ru"]


async def test_a_code_still_passes_through(monkeypatch, tmp_path):
    assert await _lang_reaching_the_provider(monkeypatch, tmp_path, "ru") == ["ru"]


async def test_nothing_pinned_is_still_detect(monkeypatch, tmp_path):
    """The original bug must not come back through the conversion."""
    assert await _lang_reaching_the_provider(monkeypatch, tmp_path, None) == [""]
    assert await _lang_reaching_the_provider(monkeypatch, tmp_path, "auto") == [""]


async def test_an_unmappable_name_degrades_to_detect(monkeypatch, tmp_path):
    """Detection is what these providers do well; a refused pin is total failure."""
    assert await _lang_reaching_the_provider(monkeypatch, tmp_path, "Klingon") == [""]
