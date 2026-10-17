"""
Speech-to-Text Module for NAVIG

Multi-provider STT with support for Deepgram, OpenAI Whisper,
and local Whisper models.

Usage:
    from navig_audio.voice.stt import transcribe, STT

    # Simple usage
    text = await transcribe("audio.wav")

    # With provider selection
    stt = STT(provider="deepgram")
    result = await stt.transcribe("audio.mp3")
    print(result.text)

Providers:
- deepgram: Real-time & batch, requires DEEPGRAM_API_KEY
- whisper_api: OpenAI Whisper API, requires OPENAI_API_KEY
- whisper_local: Local Whisper model (offline, requires whisper package)
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path


# =============================================================================
# Types
# =============================================================================


class STTProvider(str, Enum):
    """Supported STT providers."""

    DEEPGRAM = "deepgram"
    WHISPER_API = "whisper_api"
    WHISPER_LOCAL = "whisper_local"
    MIMO = "mimo"
    # ElevenLabs Scribe — same key as the TTS side; word timings + diarization,
    # billed against the ElevenLabs subscription instead of a separate STT account.
    ELEVENLABS = "elevenlabs"


@dataclass
class STTConfig:
    """STT configuration."""

    provider: STTProvider = STTProvider.WHISPER_API
    fallback_providers: list[STTProvider] = field(
        default_factory=lambda: [STTProvider.WHISPER_LOCAL]
    )

    # Language. EMPTY MEANS DETECT — do not put a language code here as a
    # "sensible default". This shipped as "en", and because nothing upstream
    # passed a language, every transcription in navig was *told* the audio was
    # English: Russian speech came back as English-looking nonsense, and any
    # briefing built on that transcript inherited the wrong language too. A
    # transcriber that is guessing must be allowed to guess.
    language: str = ""
    detect_language: bool = False

    # Deepgram settings
    deepgram_model: str = "nova-2"
    deepgram_tier: str = "enhanced"
    deepgram_punctuate: bool = True
    deepgram_diarize: bool = False

    # Whisper API settings
    whisper_model: str = "whisper-1"

    # Whisper local settings
    whisper_local_model: str = "base"  # tiny, base, small, medium, large

    # Xiaomi MiMo ASR settings — official endpoint, $0.074 per hour of audio.
    # The API accepts mp3/wav ONLY (data URL or base64+format), so other
    # containers (Telegram's .ogg/Opus) are converted before the call.
    mimo_model: str = "mimo-v2.5-asr"

    # General
    timeout_seconds: int = 60  # network providers (Deepgram / Whisper API)
    # Local Whisper runs on the CPU in a worker thread — far slower than a network round
    # trip and legitimately minutes for a large file/model — so it gets its own, generous
    # bound. It exists to catch a genuine HANG (a stalled model download, a wedged
    # transcribe), not to enforce a tight SLA; raise it if you transcribe long files on a
    # big model. `0` disables the bound (the old unbounded behaviour).
    whisper_local_timeout_seconds: int = 600
    max_audio_size_mb: int = 25


@dataclass
class STTResult:
    """Result from STT transcription."""

    success: bool
    text: str | None = None

    # Metadata
    provider: STTProvider | None = None
    language: str | None = None
    confidence: float | None = None
    duration_ms: int | None = None
    latency_ms: int | None = None

    # Segments (if available)
    segments: list[dict] | None = None

    # Error
    error: str | None = None

    def __bool__(self) -> bool:
        return self.success


# =============================================================================
# STT Engine
# =============================================================================


class STT:
    """
    Speech-to-Text engine with multi-provider support.

    Supports Deepgram, OpenAI Whisper API, and local Whisper models
    with automatic fallback.
    """

    def __init__(self, config: STTConfig | None = None):
        self.config = config or STTConfig()

    @staticmethod
    def _resolve_api_key(vault_label: str, env_var: str) -> str | None:
        """Resolve an API key from the vault (by provider), then os.environ.

        Keys added with ``navig vault set <provider> <key>`` are stored under the
        PROVIDER, not a literal ``provider/api-key`` label — so resolve through the
        vault's canonical ``get_api_key(provider)`` (which handles api_key / token /
        value), with a few explicit-label fallbacks for older entries, before the
        env-var fallback. Accepts a ``provider/data_key`` style ``vault_label`` and
        uses its leading segment as the provider.
        """
        from navig_media_core.tools.media_providers import resolve_media_key  # noqa: PLC0415

        provider = vault_label.split("/", 1)[0]
        # Fallbacks for explicit secret labels (provider-only, underscore, hyphen).
        return resolve_media_key(
            provider, env_var, prefer="vault",
            labels=(provider, f"{provider}/api_key", vault_label), caller="voice_stt",
        )

    async def transcribe(
        self,
        audio_path: str | Path,
        provider: STTProvider | None = None,
        language: str | None = None,
        **kwargs,
    ) -> STTResult:
        """
        Transcribe audio to text.

        Args:
            audio_path: Path to audio file (MP3, WAV, FLAC, etc.)
            provider: Override provider
            language: Override language code
            **kwargs: Provider-specific options

        Returns:
            STTResult with transcribed text or error
        """
        start_time = datetime.now()  # utcnow() deprecated in Py3.12+
        audio_path = Path(audio_path)

        if not audio_path.exists():
            return STTResult(success=False, error=f"Audio file not found: {audio_path}")

        # Check file size
        size_mb = audio_path.stat().st_size / (1024 * 1024)
        if size_mb > self.config.max_audio_size_mb:
            return STTResult(
                success=False,
                error=(
                    f"Audio file too large: {size_mb:.1f}MB (max {self.config.max_audio_size_mb}MB)"
                ),
            )

        # "" / "auto" ⇒ let the provider detect. Callers that pin a language get
        # it honoured; callers that pin nothing must NOT inherit someone's guess.
        #
        # The pin arrives as a HUMAN-written name — `user.language: Russian` — and
        # every provider below wants an ISO code: Deepgram and the Whisper API
        # reject a name, local Whisper refuses it outright. So a pinned language
        # used to be strictly worse than pinning nothing, which is the opposite of
        # what a preference is for. `language_code` maps the name onto the code
        # and degrades an unrecognised one to "" (detect) rather than to a value
        # the provider will refuse. This is the SECOND STT entry point — the video
        # transcript path — and it needs the conversion as much as the first.
        from navig_sdk.lang import language_code

        lang = language_code(language or self.config.language) or ""
        providers = (
            [provider]
            if provider
            else [self.config.provider] + self.config.fallback_providers
        )

        last_error = None
        for prov in providers:
            if prov is None:
                continue
            try:
                result = await self._transcribe_with_provider(
                    audio_path, prov, lang, **kwargs
                )
                if result.success:
                    result.latency_ms = int(
                        (datetime.now() - start_time).total_seconds() * 1000
                    )
                    return result
                else:
                    last_error = result.error
            except Exception as e:
                last_error = str(e)
                continue

        return STTResult(
            success=False,
            error=f"All STT providers failed. Last error: {last_error}",
        )

    # =========================================================================
    # Provider Implementations
    # =========================================================================

    async def _transcribe_with_provider(
        self,
        audio_path: Path,
        provider: STTProvider,
        language: str,
        **kwargs,
    ) -> STTResult:
        """Dispatch to provider-specific implementation."""
        if provider == STTProvider.DEEPGRAM:
            return await self._transcribe_deepgram(audio_path, language, **kwargs)
        elif provider == STTProvider.WHISPER_API:
            return await self._transcribe_whisper_api(audio_path, language, **kwargs)
        elif provider == STTProvider.WHISPER_LOCAL:
            return await self._transcribe_whisper_local(audio_path, language, **kwargs)
        elif provider == STTProvider.MIMO:
            return await self._transcribe_mimo(audio_path, language, **kwargs)
        elif provider == STTProvider.ELEVENLABS:
            return await self._transcribe_elevenlabs(audio_path, language, **kwargs)
        else:
            return STTResult(success=False, error=f"Unknown STT provider: {provider}")

    async def _transcribe_elevenlabs(
        self, audio_path: Path, language: str, **kwargs
    ) -> STTResult:
        """ElevenLabs Scribe (`POST /v1/speech-to-text`) — the ElevenLabs key only.

        No language means Scribe detects (this module's "" = detect rule); word
        timings come back in the same payload, so captions cut from them line up
        with the audio rather than with an estimate.
        """
        try:
            from navig_generate.tools.audio_generation import (  # noqa: PLC0415
                AudioGenerationConfig,
                AudioGenerator,
            )
        except ImportError:
            return STTResult(
                success=False, error="navig-generate not installed (Scribe unavailable)"
            )
        gen = AudioGenerator(AudioGenerationConfig(save_locally=False))
        start = datetime.now()
        try:
            data = await gen.transcribe(audio_path, language_code=language or None)
        except Exception as exc:  # noqa: BLE001 — a provider failure is a RESULT here
            return STTResult(success=False, error=str(exc), provider=STTProvider.ELEVENLABS)
        finally:
            await gen.close()
        words = data.get("words") or []
        return STTResult(
            success=True,
            text=str(data.get("text") or ""),
            provider=STTProvider.ELEVENLABS,
            language=data.get("language_code"),
            duration_ms=int((datetime.now() - start).total_seconds() * 1000),
            segments=[{"word": w.get("text"), "start": w.get("start"),
                       "end": w.get("end")} for w in words if isinstance(w, dict)] or None,
        )

    async def _transcribe_deepgram(
        self, audio_path: Path, language: str, **kwargs
    ) -> STTResult:
        """Transcribe using Deepgram API."""
        try:
            import aiohttp
        except ImportError:
            return STTResult(success=False, error="aiohttp not installed")

        api_key = self._resolve_api_key("deepgram/api-key", "DEEPGRAM_API_KEY")
        if not api_key:
            return STTResult(success=False, error="DEEPGRAM_API_KEY not set")

        url = "https://api.deepgram.com/v1/listen"
        params = {
            "model": self.config.deepgram_model,
            "tier": self.config.deepgram_tier,
            "language": language,
            "punctuate": str(self.config.deepgram_punctuate).lower(),
            "diarize": str(self.config.deepgram_diarize).lower(),
        }
        if self.config.detect_language or not language:
            # Nothing pinned ⇒ ask Deepgram to detect rather than sending a blank
            # (or inherited) language it would reject or misapply.
            params["detect_language"] = "true"
            params.pop("language", None)

        headers = {
            "Authorization": f"Token {api_key}",
            "Content-Type": self._get_content_type(audio_path),
        }

        try:
            audio_data = audio_path.read_bytes()
            async with (
                aiohttp.ClientSession() as session,
                session.post(
                    url,
                    params=params,
                    headers=headers,
                    data=audio_data,
                    timeout=aiohttp.ClientTimeout(total=self.config.timeout_seconds),
                ) as response,
            ):
                if response.status != 200:
                    error_text = await response.text()
                    return STTResult(
                        success=False,
                        error=f"Deepgram API error {response.status}: {error_text}",
                    )

                data = await response.json()
                channel = data.get("results", {}).get("channels", [{}])[0]
                alt = channel.get("alternatives", [{}])[0]
                text = alt.get("transcript", "")
                confidence = alt.get("confidence", 0.0)
                detected_lang = (
                    data.get("results", {})
                    .get("channels", [{}])[0]
                    .get("detected_language", language)
                )

                return STTResult(
                    success=bool(text),
                    text=text or None,
                    provider=STTProvider.DEEPGRAM,
                    language=detected_lang,
                    confidence=confidence,
                )
        except asyncio.TimeoutError:
            return STTResult(success=False, error="Deepgram transcription timeout")
        except Exception as e:
            return STTResult(success=False, error=f"Deepgram error: {e}")

    async def _transcribe_whisper_api(
        self, audio_path: Path, language: str, **kwargs
    ) -> STTResult:
        """Transcribe using OpenAI Whisper API."""
        try:
            import aiohttp
        except ImportError:
            return STTResult(success=False, error="aiohttp not installed")

        api_key = self._resolve_api_key("openai/api-key", "OPENAI_API_KEY")
        if not api_key:
            return STTResult(success=False, error="OPENAI_API_KEY not set")

        url = "https://api.openai.com/v1/audio/transcriptions"
        headers = {"Authorization": f"Bearer {api_key}"}

        try:
            data = aiohttp.FormData()
            data.add_field(
                "file",
                audio_path.read_bytes(),
                filename=audio_path.name,
                content_type=self._get_content_type(audio_path),
            )
            data.add_field("model", self.config.whisper_model)
            # Omit the field entirely when nothing is pinned — Whisper detects the
            # language itself, and sending a wrong one silently corrupts the text.
            if language and not self.config.detect_language:
                data.add_field("language", language)
            data.add_field("response_format", "verbose_json")

            async with (
                aiohttp.ClientSession() as session,
                session.post(
                    url,
                    headers=headers,
                    data=data,
                    timeout=aiohttp.ClientTimeout(total=self.config.timeout_seconds),
                ) as response,
            ):
                if response.status != 200:
                    error_text = await response.text()
                    return STTResult(
                        success=False,
                        error=f"Whisper API error {response.status}: {error_text}",
                    )

                result_data = await response.json()
                text = result_data.get("text", "")
                detected_lang = result_data.get("language", language)
                duration = result_data.get("duration")
                segments = result_data.get("segments")

                return STTResult(
                    success=bool(text),
                    text=text or None,
                    provider=STTProvider.WHISPER_API,
                    language=detected_lang,
                    duration_ms=int(duration * 1000) if duration else None,
                    segments=segments,
                )
        except asyncio.TimeoutError:
            return STTResult(success=False, error="Whisper API timeout")
        except Exception as e:
            return STTResult(success=False, error=f"Whisper API error: {e}")

    async def _transcribe_mimo(
        self, audio_path: Path, language: str, **kwargs
    ) -> STTResult:
        """Transcribe via Xiaomi MiMo ASR (official endpoint, chat-completions shape).

        Measured live 2026-10-13: ``POST /v1/chat/completions`` with
        ``model=mimo-v2.5-asr`` and an ``input_audio`` content part returns the
        transcript in ``choices[0].message.content`` (round-tripped a generated
        mp3 back correctly). The API accepts **mp3/wav only** — Telegram voice
        is .ogg/Opus, so other containers are converted with ffmpeg first.
        Price: $0.074 per hour of audio.
        """
        try:
            import aiohttp
        except ImportError:
            return STTResult(success=False, error="aiohttp not installed")

        api_key = self._resolve_api_key("mimo/api-key", "MIMO_API_KEY")
        if not api_key:
            return STTResult(success=False, error="MIMO_API_KEY not set")

        # mp3/wav pass through; everything else (ogg, webm, flac, m4a) is
        # converted. A missing ffmpeg here is a named, fixable error — the
        # whole provider path depends on it for Telegram's container. The
        # subprocess runs in a WORKER: ffmpeg on the daemon's single event
        # loop would freeze the whole gateway (deck, webhook, uplink) for the
        # duration of the conversion (source-guard: no blocking calls on the
        # event loop).
        send_path = audio_path
        tmp_wav: Path | None = None
        suffix = audio_path.suffix.lower()
        if suffix not in (".mp3", ".wav"):
            try:
                import subprocess
                import tempfile

                fd, tmp_name = tempfile.mkstemp(suffix=".wav")
                os.close(fd)
                tmp_wav = Path(tmp_name)
                proc = await asyncio.to_thread(
                    subprocess.run,
                    ["ffmpeg", "-y", "-i", str(audio_path), "-ar", "16000", "-ac", "1", str(tmp_wav)],
                    capture_output=True,
                    timeout=60,
                )
                if proc.returncode != 0 or not tmp_wav.exists() or tmp_wav.stat().st_size == 0:
                    return STTResult(
                        success=False,
                        error=f"ffmpeg conversion failed: {proc.stderr.decode(errors='ignore')[:160]}",
                    )
                send_path = tmp_wav
            except FileNotFoundError:
                return STTResult(
                    success=False,
                    error="ffmpeg not found — MiMo ASR needs it to convert Telegram .ogg audio",
                )
            except Exception as e:
                return STTResult(success=False, error=f"audio conversion failed: {e}")

        try:
            import base64

            b64 = base64.b64encode(send_path.read_bytes()).decode()
            fmt = send_path.suffix.lstrip(".").lower()
            body = {
                "model": self.config.mimo_model,
                "max_tokens": 4096,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_audio", "input_audio": {"data": b64, "format": fmt}}
                        ],
                    }
                ],
            }
            url = "https://api.xiaomimimo.com/v1/chat/completions"
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            async with (
                aiohttp.ClientSession() as session,
                session.post(
                    url,
                    headers=headers,
                    json=body,
                    timeout=aiohttp.ClientTimeout(total=self.config.timeout_seconds),
                ) as response,
            ):
                if response.status != 200:
                    error_text = await response.text()
                    return STTResult(
                        success=False,
                        error=f"MiMo ASR error {response.status}: {error_text[:200]}",
                    )
                data = await response.json()
                text = (
                    ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                ).strip()
                return STTResult(
                    success=bool(text),
                    text=text or None,
                    provider=STTProvider.MIMO,
                    # The API returns no language field — auto-detect claimed
                    # nothing rather than inventing a code.
                    language=None,
                )
        except asyncio.TimeoutError:
            return STTResult(success=False, error="MiMo ASR timeout")
        except Exception as e:
            return STTResult(success=False, error=f"MiMo ASR error: {e}")
        finally:
            if tmp_wav is not None:
                try:
                    tmp_wav.unlink(missing_ok=True)
                except OSError:
                    pass

    async def _transcribe_whisper_local(
        self, audio_path: Path, language: str, **kwargs
    ) -> STTResult:
        """Transcribe using a local Whisper model (offline).

        faster-whisper first: it is the package navig-audio installs, and it is several times
        faster on CPU. openai-whisper (``import whisper``) is used when only it is present. This
        used to import openai-whisper alone, which navig-audio never declared, so offline
        transcription failed on every plain install with the dependency it did ship unused.
        """
        engine = _local_whisper_engine()
        if engine is None:
            return STTResult(
                success=False,
                error="local Whisper not installed. Install with: pip install faster-whisper",
            )

        model_name = kwargs.get("model", self.config.whisper_local_model)
        # None ⇒ Whisper detects. Nothing pinned must reach it as None, not as an inherited code.
        pinned = None if (self.config.detect_language or not language) else language

        try:
            loop = asyncio.get_running_loop()

            def _run():
                if engine == "faster_whisper":
                    from faster_whisper import WhisperModel  # noqa: PLC0415

                    def _segments(device: str):
                        model, lock = _cached_whisper(WhisperModel, model_name, device)
                        with lock:  # one decode per loaded model at a time
                            seg_iter, info = model.transcribe(
                                str(audio_path), language=pinned, vad_filter=True
                            )
                            # The generator decodes lazily, so a GPU failure surfaces HERE.
                            return [
                                {"start": s.start, "end": s.end, "text": s.text.strip()}
                                for s in seg_iter
                            ], info

                    try:
                        segments, info = _segments("auto")
                    except (RuntimeError, OSError) as exc:
                        # "auto" picks CUDA whenever a GPU driver is present, even when the
                        # CUDA libraries (cuBLAS/cuDNN) are not: then every call fails. CPU works.
                        if not any(k in str(exc).lower() for k in ("cuda", "cublas", "cudnn")):
                            raise
                        segments, info = _segments("cpu")
                    return {
                        "text": " ".join(s["text"] for s in segments),
                        "language": info.language or language,
                        "segments": segments,
                    }
                import whisper  # noqa: PLC0415

                return whisper.load_model(model_name).transcribe(str(audio_path), language=pinned)

            # Bound the blocking transcription so a stalled model download or a wedged
            # transcribe() can't hang the caller forever (the network providers already
            # honour a timeout; local Whisper had none). On timeout the worker thread
            # can't be cancelled — Python can't kill a thread — so it is abandoned to
            # finish in the background, but the pipeline gets a clean error and can fall
            # back / move on instead of wedging. `<= 0` keeps the old unbounded behaviour.
            fut = loop.run_in_executor(None, _run)
            budget = self.config.whisper_local_timeout_seconds
            result = await (asyncio.wait_for(fut, timeout=budget) if budget and budget > 0 else fut)
            text = result.get("text", "").strip()
            detected_lang = result.get("language", language)
            segments = result.get("segments")

            return STTResult(
                success=bool(text),
                text=text or None,
                provider=STTProvider.WHISPER_LOCAL,
                language=detected_lang,
                segments=segments,
            )
        except asyncio.TimeoutError:
            return STTResult(
                success=False,
                error=(
                    "Whisper local transcription timed out after "
                    f"{self.config.whisper_local_timeout_seconds}s "
                    "(raise stt whisper_local_timeout_seconds for long files)"
                ),
            )
        except Exception as e:
            return STTResult(success=False, error=f"Whisper local error: {e}")

    # =========================================================================
    # Helpers
    # =========================================================================

    @staticmethod
    def _get_content_type(path: Path) -> str:
        """Get MIME type for audio file."""
        ext = path.suffix.lower()
        return {
            ".mp3": "audio/mpeg",
            ".wav": "audio/wav",
            ".flac": "audio/flac",
            ".ogg": "audio/ogg",
            ".m4a": "audio/mp4",
            ".webm": "audio/webm",
        }.get(ext, "application/octet-stream")

    async def list_models(self, provider: STTProvider | None = None) -> list[dict]:
        """List available models for a provider."""
        prov = provider or self.config.provider

        if prov == STTProvider.DEEPGRAM:
            return [
                {
                    "id": "nova-2",
                    "name": "Nova 2",
                    "description": "Latest, most accurate",
                },
                {"id": "nova", "name": "Nova", "description": "Fast and accurate"},
                {"id": "enhanced", "name": "Enhanced", "description": "High accuracy"},
                {"id": "base", "name": "Base", "description": "Standard quality"},
            ]
        elif prov == STTProvider.WHISPER_API:
            return [
                {
                    "id": "whisper-1",
                    "name": "Whisper v1",
                    "description": "OpenAI Whisper",
                }
            ]
        elif prov == STTProvider.WHISPER_LOCAL:
            return [
                {"id": "tiny", "name": "Tiny", "description": "39M params, fastest"},
                {"id": "base", "name": "Base", "description": "74M params"},
                {"id": "small", "name": "Small", "description": "244M params"},
                {"id": "medium", "name": "Medium", "description": "769M params"},
                {
                    "id": "large",
                    "name": "Large",
                    "description": "1550M params, most accurate",
                },
            ]
        return []


# =============================================================================
# Module-level convenience
# =============================================================================

_default_stt: STT | None = None


def _resolve_audio_file_params(
    filename: str, *, is_voice: bool = False
) -> tuple[str, str]:
    """Return a normalized upload filename and MIME type for an audio file.

    Voice messages are normalized to Telegram's preferred ``.oga`` filename
    while preserving the Ogg MIME type.
    """
    path = Path(filename)

    if is_voice:
        return "voice.oga", "audio/ogg"

    mime, _ = mimetypes.guess_type(str(path))
    if mime:
        return path.name, mime

    mime = STT._get_content_type(path)
    return path.name, mime


#: Loaded faster-whisper models, newest last. Loading costs 1.4 s (base) to 6 s (medium) — on
#: every dictation that was the bulk of the wait. Two models at most stay in memory.
_WHISPER_MODELS: dict[tuple[str, str], tuple[object, object]] = {}
_WHISPER_KEEP = 2
_WHISPER_GUARD = threading.Lock()


def _cached_whisper(factory, model_name: str, device: str):  # noqa: ANN001, ANN202
    """``(model, lock)`` for ``model_name`` on ``device``, loading it once."""
    key = (model_name, device)
    with _WHISPER_GUARD:
        hit = _WHISPER_MODELS.pop(key, None)
        if hit is None:
            hit = (factory(model_name, device=device, compute_type="int8"), threading.Lock())
        _WHISPER_MODELS[key] = hit  # most recently used last
        while len(_WHISPER_MODELS) > _WHISPER_KEEP:
            _WHISPER_MODELS.pop(next(iter(_WHISPER_MODELS)))
        return hit


def _local_whisper_engine() -> str | None:
    """``"faster_whisper"``, ``"whisper"`` (openai-whisper), or ``None`` when neither imports."""
    import importlib.util  # noqa: PLC0415

    for engine in ("faster_whisper", "whisper"):
        if engine in sys.modules:  # already imported (or blocked with None)
            if sys.modules[engine] is not None:
                return engine
            continue
        try:
            if importlib.util.find_spec(engine) is not None:
                return engine
        except (ImportError, ValueError):
            continue
    return None


def whisper_local_available() -> bool:
    """True if an offline Whisper (faster-whisper or openai-whisper) is installed.

    Used by the voice handlers to detect the no-API-key local backend. (Was
    imported by callers but never defined here — the import silently failed,
    so local Whisper was never offered.)
    """
    return _local_whisper_engine() is not None


def get_stt() -> STT:
    """Get default STT instance."""
    global _default_stt
    if _default_stt is None:
        _default_stt = STT()
    return _default_stt


async def transcribe(audio_path: str | Path, **kwargs) -> str | None:
    """Transcribe audio file to text. Returns None on failure."""
    result = await get_stt().transcribe(audio_path, **kwargs)
    return result.text if result.success else None


async def transcribe_full(audio_path: str | Path, **kwargs) -> STTResult:
    """Transcribe audio and return full result."""
    return await get_stt().transcribe(audio_path, **kwargs)


def transcribe_sync(audio_path: str | Path, **kwargs) -> str | None:
    """Synchronous wrapper for transcribe()."""
    return asyncio.run(transcribe(audio_path, **kwargs))
