"""Dictation: which speech-to-text engine may hear you, and what is done to the words.

Routing follows the same voice policy as reading aloud (``voice.policy``):

* ``offline`` / ``free`` — on-device Whisper only; the recording never leaves the machine.
* ``auto``    — on-device Whisper; a cloud engine only when you name it.
* ``premium`` — your keyed cloud engine (ElevenLabs Scribe, OpenAI Whisper, Deepgram, MiMo),
  else on-device Whisper (said).

Modes (:data:`MODES`) shape the text *after* recognition. ``verbatim`` returns exactly what
was recognised — nothing is rewritten. ``clean`` and ``no_fillers`` are deterministic edits
(spacing, capitals, filler words). ``notes`` and ``prompt`` need a language model; the gateway
runs those through NAVIG's LLM seam — this module only says which modes need one.
"""

from __future__ import annotations

import importlib.util
import re
from dataclasses import dataclass

from navig_audio.voice.stt import STT, STTProvider

MODES = ("verbatim", "clean", "no_fillers", "notes", "prompt")
LLM_MODES = ("notes", "prompt")
LOCAL_MODELS = ("tiny", "base", "small", "medium")

#: Premium order: best fr/ru/en accuracy first.
_CLOUD_ORDER = (STTProvider.ELEVENLABS, STTProvider.WHISPER_API, STTProvider.DEEPGRAM, STTProvider.MIMO)
_CLOUD_KEYS = {
    STTProvider.ELEVENLABS: ("elevenlabs", "ELEVENLABS_API_KEY", "XI_API_KEY"),
    STTProvider.WHISPER_API: ("openai", "OPENAI_API_KEY"),
    STTProvider.DEEPGRAM: ("deepgram", "DEEPGRAM_API_KEY"),
    STTProvider.MIMO: ("mimo", "MIMO_API_KEY"),
}


class DictationRoutingError(ValueError):
    """No engine the policy allows can transcribe here."""


@dataclass(frozen=True)
class SttRoute:
    provider: STTProvider
    on_device: bool
    note: str = ""


def local_available() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None or importlib.util.find_spec("whisper") is not None


def _keyed(provider: STTProvider) -> bool:
    from navig_audio.voice.tts import TTS  # the shared vault → env key resolution

    prov, *env = _CLOUD_KEYS[provider]
    return bool(TTS._resolve_api_key(prov, *env))


def route_stt(policy: str, requested: STTProvider | None = None) -> SttRoute:
    """The engine for one dictation under ``policy``. Raises :class:`DictationRoutingError`."""
    policy = policy if policy in ("auto", "free", "premium", "offline") else "auto"
    if requested is not None and requested is not STTProvider.WHISPER_LOCAL:
        if policy in ("free", "offline"):
            raise DictationRoutingError(
                f"{'Offline' if policy == 'offline' else 'Free'} mode keeps dictation on this machine — "
                f"{requested.value} is a cloud service"
            )
        if requested not in _CLOUD_KEYS or not _keyed(requested):
            raise DictationRoutingError(f"{requested.value} has no API key in the vault")
        return SttRoute(requested, False)
    if policy == "premium" and requested is None:
        for prov in _CLOUD_ORDER:
            if _keyed(prov):
                return SttRoute(prov, False)
        if local_available():
            return SttRoute(STTProvider.WHISPER_LOCAL, True, "Premium mode: no cloud speech key — using on-device Whisper")
    if local_available():
        return SttRoute(STTProvider.WHISPER_LOCAL, True)
    raise DictationRoutingError(
        "On-device speech recognition is not installed (pip install faster-whisper)"
        + ("" if policy in ("free", "offline") else " — or name a cloud engine you have a key for")
    )


def make_stt(route: SttRoute, *, local_model: str = "base") -> STT:
    """An STT engine pinned to ``route`` — no fallback chain (a paid fallback is never silent)."""
    from navig_audio.voice.stt import STTConfig

    model = local_model if local_model in LOCAL_MODELS else "base"
    return STT(STTConfig(provider=route.provider, fallback_providers=[], whisper_local_model=model))


def warm(local_model: str = "base") -> bool:
    """Import faster-whisper and load the dictation model now, so the words come back fast.

    Measured cold on the dev box: the import alone is ~9 s (CTranslate2, PyAV, ONNX runtime)
    and the model ~1.7 s — paid on the first dictation unless something warms it while the
    person is still speaking. Idempotent: a loaded model is reused (stt._cached_whisper).
    Returns False when there is nothing local to warm.
    """
    if importlib.util.find_spec("faster_whisper") is None:
        return False
    from faster_whisper import WhisperModel

    from navig_audio.voice.stt import _cached_whisper

    model = local_model if local_model in LOCAL_MODELS else "base"
    try:
        _cached_whisper(WhisperModel, model, "auto")
    except (RuntimeError, OSError) as exc:
        # Same rule as transcription: a GPU driver without its CUDA libraries → CPU.
        if not any(k in str(exc).lower() for k in ("cuda", "cublas", "cudnn")):
            raise
        _cached_whisper(WhisperModel, model, "cpu")
    return True


# ── deterministic modes ────────────────────────────────────────────────────────

_FILLERS = {
    "en": r"(?:um+|uh+|erm+|er|hmm+|mm+|uh-huh)",
    "fr": r"(?:euh+|heu+|hum+|bah|mmh+)",
    "ru": r"(?:э+|ээ+|э-э+|эм+|мм+|кхм)",
}
_SENTENCE_END = re.compile(r"([.!?…]\s+)(\w)", re.UNICODE)


def _clean(text: str, lang: str | None) -> str:
    t = re.sub(r"\s+", " ", text).strip()
    t = re.sub(r"\s+([,.;:!?…])", r"\1", t)  # no space before punctuation…
    t = re.sub(r"([,;:])(?:\s*[,;:])+", r"\1", t)  # a removed word leaves no doubled comma
    t = re.sub(r"^[,;:]\s*", "", t)
    if lang == "fr":  # …except French high punctuation, which takes one
        t = re.sub(r"(?<=\w)([;:!?])", r" \1", t)
    t = re.sub(r"([,;:])(?=\w)", r"\1 ", t)
    t = _SENTENCE_END.sub(lambda m: m.group(1) + m.group(2).upper(), t)
    if t[:1].islower():
        t = t[0].upper() + t[1:]
    return t


def _remove_fillers(text: str, lang: str | None) -> str:
    langs = [lang] if lang in _FILLERS else list(_FILLERS)
    for code in langs:
        pat = re.compile(rf"(?<!\w){_FILLERS[code]}(?!\w)[,.]?\s*", re.IGNORECASE | re.UNICODE)
        text = pat.sub("", text)
    return text


def shape(text: str, mode: str, lang: str | None = None) -> str:
    """Apply a deterministic mode. ``verbatim`` only trims the ends — never a rewrite.

    LLM modes are not handled here (the gateway runs them); passing one returns the
    ``clean`` form, which is what an LLM mode falls back to when no model answers.
    """
    if mode == "verbatim":
        return text.strip()
    if mode == "no_fillers":
        return _clean(_remove_fillers(text, lang), lang)
    return _clean(text, lang)
