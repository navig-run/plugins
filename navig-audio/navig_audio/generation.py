"""AI audio generation — the audio **facet** of the shared media engine.

Core keeps ONE modality-parameterized generation engine
(``navig.media.generation_service``). Rather than clone that orchestrator per
media type, each facet registers a per-modality backend into the engine's
registry (``navig.media.types.register_generator``). This module is the
**audio** facet: it wraps core's ElevenLabs client
(``navig.tools.audio_generation``) and plugs it in as the
:data:`~navig.media.types.MediaModality.AUDIO` backend — so
``navig generate gen --modality audio`` and the deck ``/api/deck/media`` route run
through navig-audio when it's installed, and fall back to core's built-in audio
backend when it isn't (identical output either way).

The provider client itself deliberately stays in core: it's a thin,
dependency-light HTTP client that the agent tool packs
(``navig.tools.domains.audio_pack``) import directly, so moving it would break
them. The facet owns the *wiring* and the ``navig audio gen`` verb, not the
transport. The one behavioural improvement over the built-in branch: this
backend honours ``n`` (the built-in always produced a single clip).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from navig.tools.audio_generation import GeneratedAudio

_log = logging.getLogger(__name__)
_REGISTERED = False


async def _audio_backend(
    enriched_prompt: str,
    *,
    provider: str | None = None,
    size: str | None = None,  # unused for audio; part of the shared backend contract
    kind: str = "music",
    duration_s: float | None = None,
    n: int = 1,
    seed: int | None = None,  # ElevenLabs audio has no seed; accepted then ignored
    out_dir: Path,
    **_ignored: Any,
) -> list[GeneratedAudio]:
    """Generate ``n`` audio clip(s) into ``out_dir``; return the result objects.

    Matches the engine's ``GeneratorBackend`` signature
    (``navig.media.types.GeneratorBackend``). Each clip is written under a
    unique name so an ``n>1`` batch never collides (the underlying client names
    files by timestamp, which repeats within the same second).
    """
    from navig.tools.audio_generation import AudioGenerationConfig, AudioGenerator

    cfg = AudioGenerationConfig.from_env()
    cfg.output_dir = str(out_dir)
    gen = AudioGenerator(cfg)
    results: list[GeneratedAudio] = []
    try:
        for i in range(max(1, n)):
            aud = await gen.generate(enriched_prompt, kind=kind, duration_s=duration_s)
            # If the client reported a path but no file exists, that clip failed — drop
            # it rather than return a dead path. This applies to a SINGLE clip too: the
            # batch path already guarded it, but an n==1 failure slipped through as a
            # fileless "success" that a caller would report as generated.
            if not aud.local_path or not Path(aud.local_path).exists():
                _log.warning("navig-audio: clip %d produced no file; dropping", i)
                continue
            if n > 1:
                # Uniquify each clip (the client names files by timestamp, which
                # repeats within a second) so an n>1 batch never collides on disk.
                src = Path(aud.local_path)
                dst = src.with_name(f"{src.stem}_{i:02d}{src.suffix}")
                src.replace(dst)
                aud.local_path = str(dst)
            results.append(aud)
    finally:
        await gen.close()
    return results


def register_audio_facet() -> None:
    """Register the audio backend into core's generation registry (idempotent).

    Degrades to a no-op when core is too old to expose the registry — the engine
    then uses its built-in audio branch, so nothing breaks.
    """
    global _REGISTERED
    if _REGISTERED:
        return
    try:
        from navig.media.types import MediaModality, register_generator

        register_generator(MediaModality.AUDIO, _audio_backend)
        _REGISTERED = True
        _log.debug("navig-audio: registered AUDIO generation facet")
    except Exception as exc:  # pragma: no cover - defensive (old core / import order)
        _log.debug("navig-audio: audio facet not registered (%s)", exc)
