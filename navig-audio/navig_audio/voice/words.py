"""Transcribe with word-level timings — the answer captions need.

:mod:`navig_audio.voice.stt` answers "what was said". Captions also need *when*: each word's
start and end is the ``.srt`` that gets burned in, and how a passage of a long track is found.
:func:`transcribe_words` returns Whisper's ``verbose_json`` shape either way:

    {"text": str, "language": str, "duration": float,
     "words": [{"word": str, "start": float, "end": float}, ...],
     "segments": [{"text": str, "start": float, "end": float}, ...]}

Two engines:

* ``"whisper_api"`` — OpenAI's Whisper with ``timestamp_granularities=word``. Needs an OpenAI
  key (the shared vault, then ``OPENAI_API_KEY``); about $0.006 a minute; 25 MB per request.
* ``"whisper_local"`` — faster-whisper on this machine with ``word_timestamps=True``. No key,
  no upload limit, no cost; slower, and the first run downloads the model.

``engine=None`` picks the API when a key resolves and local otherwise, so captions work with
nothing configured at all. Failures raise :class:`WordsError` with a message a person can act
on — never an empty result, because "no words" cannot tell a silent track from a missing key.

**Language is detected, never assumed.** Forcing the wrong language does not produce a bad
transcript, it produces a plausible-looking one that is not. Pass ``language`` only when known.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = ["API_MAX_UPLOAD_MB", "WordsError", "openai_key", "transcribe_words"]

#: Whisper API's per-request cap.
API_MAX_UPLOAD_MB = 25


class WordsError(RuntimeError):
    """Transcription with timings failed; the message says what to do about it."""


def openai_key() -> str | None:
    """The OpenAI key STT uses: the shared vault first, then ``OPENAI_API_KEY``."""
    from navig_audio.voice.stt import STT  # noqa: PLC0415

    return STT._resolve_api_key("openai/api-key", "OPENAI_API_KEY")


def transcribe_words(
    audio: Path | str,
    *,
    language: str | None = None,
    engine: str | None = None,
    model: str | None = None,
    timeout: int = 600,
) -> dict[str, Any]:
    """Transcribe *audio* with word timings. See the module docstring for the shape."""
    audio = Path(audio)
    if not audio.exists():
        raise WordsError(f"audio not found: {audio}")
    if engine is None:
        engine = "whisper_api" if openai_key() else "whisper_local"
    if engine == "whisper_api":
        return _api(audio, language=language, model=model or "whisper-1", timeout=timeout)
    if engine == "whisper_local":
        return _local(audio, language=language, model=model or "base")
    raise WordsError(f"unknown engine {engine!r} (whisper_api or whisper_local)")


def _api(audio: Path, *, language: str | None, model: str, timeout: int) -> dict[str, Any]:
    import httpx  # noqa: PLC0415

    size_mb = audio.stat().st_size / (1024 * 1024)
    if size_mb > API_MAX_UPLOAD_MB:
        raise WordsError(
            f"{audio.name} is {size_mb:.1f}MB - the Whisper API accepts {API_MAX_UPLOAD_MB}MB. "
            "Cut it first, or transcribe locally (engine whisper_local has no limit)."
        )
    key = openai_key()
    if not key:
        raise WordsError(
            "no OpenAI key - store one in the vault or export OPENAI_API_KEY "
            "(~$0.006/min), or transcribe locally with faster-whisper at no cost."
        )
    data = {
        "model": model,
        "response_format": "verbose_json",
        "timestamp_granularities[]": "word",
    }
    if language:
        data["language"] = language
    try:
        with audio.open("rb") as fh:
            resp = httpx.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {key}"},
                data=data,
                files={"file": (audio.name, fh, "audio/mpeg")},
                timeout=timeout,
            )
    except httpx.HTTPError as exc:
        raise WordsError(f"transcription request failed: {exc}") from exc
    if resp.status_code != 200:
        raise WordsError(f"transcription failed ({resp.status_code}): {resp.text[:300]}")
    try:
        return resp.json()
    except json.JSONDecodeError as exc:
        raise WordsError("transcription returned something that is not JSON") from exc


def _local(audio: Path, *, language: str | None, model: str) -> dict[str, Any]:
    try:
        from faster_whisper import WhisperModel  # noqa: PLC0415
    except ImportError as exc:
        raise WordsError(
            "local transcription needs faster-whisper: pip install faster-whisper"
        ) from exc

    def _run(device: str) -> dict[str, Any]:
        whisper = WhisperModel(model, device=device, compute_type="int8")
        seg_iter, info = whisper.transcribe(
            str(audio), language=language, word_timestamps=True, vad_filter=True
        )
        words: list[dict[str, Any]] = []
        segments: list[dict[str, Any]] = []
        for seg in seg_iter:  # decodes lazily: a GPU failure surfaces here
            segments.append({"text": seg.text.strip(), "start": seg.start, "end": seg.end})
            for w in seg.words or ():
                words.append({"word": w.word.strip(), "start": w.start, "end": w.end})
        return {
            "text": " ".join(s["text"] for s in segments).strip(),
            "language": info.language or (language or "unknown"),
            "duration": float(getattr(info, "duration", 0.0) or 0.0),
            "words": words,
            "segments": segments,
        }

    try:
        return _run("auto")
    except (RuntimeError, OSError) as exc:
        # "auto" picks CUDA whenever a GPU driver is present, even without cuBLAS/cuDNN.
        if not any(k in str(exc).lower() for k in ("cuda", "cublas", "cudnn")):
            raise WordsError(f"local transcription failed: {exc}") from exc
        try:
            return _run("cpu")
        except Exception as cpu_exc:  # noqa: BLE001
            raise WordsError(f"local transcription failed: {cpu_exc}") from cpu_exc
