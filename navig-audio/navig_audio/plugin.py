"""navig-audio plugin entry point.

Provides the voice subsystem (TTS / STT / wake-word / pipeline) as a standalone
plugin. The heavy engines (edge-tts, faster-whisper, openai) live here; core keeps
thin ``navig.voice.*`` compatibility shims that re-export from ``navig_audio.voice``
(so existing consumers — the `navig voice` CLI, gateway telegram voice, agent voice
input, inbox transcription — work unchanged when this plugin is installed, and
degrade gracefully when it isn't).

Phase 3.1 also makes navig-audio the **audio facet** of core's shared generation
engine: :func:`navig_audio.generation.register_audio_facet` plugs an ``AUDIO``
backend into ``navig.media.types``, and the ``navig audio`` CLI (``gen`` /
``check``) exposes AI audio generation (music / SFX / TTS). The provider transport
stays in core; this plugin owns the wiring and the verb.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)
_REGISTERED = False


def _audio_module_def():
    from navig.modules.registry import ModuleDef, ModuleKind

    return ModuleDef(
        id="audio",
        label="Audio",
        description="Audio — voice (TTS / STT / wake-word) + AI audio generation (music / SFX / TTS).",
        kind=ModuleKind.APP,
        category="grow",
        icon="mic",
        capability=None,  # free — toggled via the module registry, not a license
        surfaces=["cli:voice", "cli:audio"],
        default_enabled=True,
        source="plugin",
    )


def register() -> None:
    """Idempotently register the ``audio`` module. Called at gateway boot."""
    global _REGISTERED
    if _REGISTERED:
        return
    try:
        from navig.modules.registry import register_module

        register_module(_audio_module_def())
    except Exception as exc:  # pragma: no cover
        _log.warning("navig-audio: module registry unavailable (%s)", exc)
    # Wire the AUDIO generation facet into core's shared generation engine.
    # Idempotent + defensive: a no-op if core predates the generator registry.
    try:
        from navig_audio.generation import register_audio_facet

        register_audio_facet()
    except Exception as exc:  # pragma: no cover
        _log.debug("navig-audio: audio facet registration skipped (%s)", exc)
    _REGISTERED = True
    _log.info("navig-audio: audio (voice + generation) module registered")
