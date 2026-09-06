"""Smoke tests for navig-audio — the voice implementation imports and the core
compat shims re-export it."""

from __future__ import annotations


def test_voice_impl_imports():
    import navig_audio.voice.stt  # noqa: F401
    import navig_audio.voice.tts  # noqa: F401
    from navig_audio.voice.stt import STT, STTConfig, STTProvider  # noqa: F401
    from navig_audio.voice.tts import TTS, TTSProvider  # noqa: F401


def test_core_compat_shims_resolve_to_plugin():
    # The core `navig.voice.*` shims must re-export the plugin implementation.
    from navig.voice.stt import STT as ShimSTT
    from navig_audio.voice.stt import STT as ImplSTT

    assert ShimSTT is ImplSTT
    assert ShimSTT.__module__ == "navig_audio.voice.stt"


def test_plugin_register_is_importable():
    from navig_audio import plugin

    assert hasattr(plugin, "register")
