"""Read a text aloud, like a book — the bedtime reader's engine.

You are in bed, you send NAVIG a text, and it reads the whole thing to you. That sentence
hides three problems this module exists to solve:

**Length.** A chapter is 10-50 thousand characters; the TTS model takes ~2,500 per request
and a request fails on its own. So the text is cut on sentence boundaries into chunks, each
told what surrounds it (``previous_text`` / ``next_text`` / ``previous_request_ids`` — the
same continuity the podcast renderer uses) so intonation carries across a seam instead of
audibly restarting. Chunks are then grouped into *parts* of about ten minutes: a single
two-hour file cannot be sent over Telegram (50 MB) and cannot be resumed.

**Cost.** ElevenLabs bills per character. A 38,000-character text is ~38,000 credits — most
of a month on a creator plan — and the person who asked is half asleep. So the plan carries
an exact estimate *before anything is spent*, every chunk is cached by content (a retry or a
re-send re-buys nothing), and the job layer refuses to start a big one unconfirmed.

**Latency.** Nobody wants to wait 8 minutes for the first word. Parts are delivered the
moment each is ready (``deliver``), so part 1 arrives while part 4 is still being generated.

Tuned for the pillow: a touch slower than conversation, a steadier delivery (less dramatic
swing than the podcast default), and loudness normalised well below the -16 LUFS podcast
standard so a sudden loud sentence never wakes you.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from navig_generate.tools.audio_generation import AudioGenerationConfig, AudioGenerator
from navig_media_core.media.audio_edit import (
    AudioEditError,
    concat,
    ffmpeg_available,
    normalize,
    probe_duration,
)

from navig_audio.podcast import cache
from navig_audio.podcast.chunker import CONTEXT_CHARS as _CONTEXT_CHARS
from navig_audio.podcast.chunker import DEFAULT_MAX_CHARS, Chunk, split_text
from navig_audio.podcast.cost import CHARS_PER_MINUTE, credit_rate
from navig_audio.podcast.scenario import DEFAULT_MODEL

__all__ = [
    "CONFIRM_CREDITS",
    "ReadError",
    "ReadOptions",
    "ReadPart",
    "ReadPlan",
    "ReadSummary",
    "clean_text",
    "decode_text_file",
    "detect_language",
    "estimate_spend",
    "options_from_config",
    "plan_reading",
    "read_aloud",
    "resolve_voice",
]

#: About how long one delivered file runs. Ten minutes is ~10 MB at 128 kbps: well inside
#: Telegram's 50 MB, short enough to resume from, long enough not to be a stream of clips.
DEFAULT_PART_MINUTES = 10.0
#: ElevenLabs accepts 0.7-1.2. A little under 1.0 is the unhurried read-aloud pace.
DEFAULT_SPEED = 0.9
#: Higher = steadier, less dramatic variation between sentences. Podcast defaults let the
#: read breathe; at night that is exactly what keeps you awake.
DEFAULT_STABILITY = 0.65
#: Well below the -16 LUFS podcast/spoken standard: even, quiet, no surprises.
DEFAULT_LOUDNESS_LUFS = -20.0
#: Silence joined between two chunks of the same part. Chunks end on sentence boundaries,
#: so a quarter second reads as an ordinary pause rather than a seam.
SILENCE_BETWEEN_CHUNKS_S = 0.25
#: Estimated credits above which a job needs an explicit yes before it starts.
CONFIRM_CREDITS = 15_000
#: A normalised part shorter than this fraction of the joined audio means loudnorm cut
#: something out; see the fallback in :func:`read_aloud`.
NORMALIZE_MIN_KEPT = 0.97

# A voice id is 20 alphanumerics; a roster name never is.
_VOICE_ID = re.compile(r"^[A-Za-z0-9]{16,32}$")


class ReadError(RuntimeError):
    """Reading could not proceed. Always says what to do about it."""


@dataclass(frozen=True)
class ReadOptions:
    """How to read. ``voice`` is an id or the NAME of one of your voices."""

    voice: str | None = None
    lang: str | None = None  # None = detect from the text
    speed: float = DEFAULT_SPEED
    stability: float = DEFAULT_STABILITY
    part_minutes: float = DEFAULT_PART_MINUTES
    model: str = DEFAULT_MODEL
    max_chunk_chars: int = DEFAULT_MAX_CHARS
    loudness_lufs: float = DEFAULT_LOUDNESS_LUFS


@dataclass(frozen=True)
class ReadPlan:
    """What a reading will be and cost — known before a single credit is spent."""

    text: str
    lang: str | None
    model: str
    speed: float
    chunks: tuple[Chunk, ...]
    parts: tuple[tuple[Chunk, ...], ...]

    @property
    def chars(self) -> int:
        return sum(c.chars for c in self.chunks)

    @property
    def credits(self) -> int:
        return round(self.chars * credit_rate(self.model))

    @property
    def minutes(self) -> float:
        return self.chars / (CHARS_PER_MINUTE * self.speed)

    @property
    def part_count(self) -> int:
        return len(self.parts)

    @property
    def slug(self) -> str:
        """Stable per text, so re-running lands in the same folder and hits the cache."""
        return "read-" + hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:10]

    def to_dict(self) -> dict[str, Any]:
        return {
            "chars": self.chars,
            "credits": self.credits,
            "minutes": round(self.minutes, 1),
            "parts": self.part_count,
            "chunks": len(self.chunks),
            "lang": self.lang,
            "model": self.model,
            "speed": self.speed,
        }


@dataclass(frozen=True)
class ReadPart:
    index: int  # 1-based
    total: int
    path: Path
    duration_s: float
    chars: int

    @property
    def caption(self) -> str:
        minutes, seconds = divmod(int(round(self.duration_s)), 60)
        return f"{self.index}/{self.total} · {minutes}:{seconds:02d}"


@dataclass
class ReadSummary:
    out_dir: Path
    parts: list[ReadPart] = field(default_factory=list)
    generated_chars: int = 0
    cached_chunks: int = 0
    generated_chunks: int = 0
    delivery_failures: int = 0
    cancelled: bool = False
    error: str | None = None
    #: Credits charged per generated character (the model's rate, set when reading starts).
    rate: float = 1.0
    #: Things that did not stop the reading but the listener should be able to learn of.
    warnings: list[str] = field(default_factory=list)

    @property
    def credits_spent(self) -> int:
        """Credits actually charged: cached chunks cost nothing, so only generated ones count."""
        return round(self.generated_chars * self.rate)

    @property
    def complete(self) -> bool:
        return not self.cancelled and self.error is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "directory": str(self.out_dir),
            "complete": self.complete,
            "cancelled": self.cancelled,
            "error": self.error,
            "generated_chars": self.generated_chars,
            "cached_chunks": self.cached_chunks,
            "generated_chunks": self.generated_chunks,
            "delivery_failures": self.delivery_failures,
            "warnings": list(self.warnings),
            "parts": [
                {
                    "index": p.index,
                    "total": p.total,
                    "file": str(p.path),
                    "duration_s": round(p.duration_s, 2),
                    "chars": p.chars,
                }
                for p in self.parts
            ],
        }


Progress = Callable[[str], None]
Deliver = Callable[[ReadPart], Awaitable[bool]]


# ── text preparation ──────────────────────────────────────────────────────────

_CODE_FENCE = re.compile(r"```.*?```", re.S)
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_URL = re.compile(r"https?://\S+|www\.\S+")
_EMPHASIS = re.compile(r"[*_`~]+")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_LINE_MARK = re.compile(r"^\s{0,3}(?:>|[-•–—]\s)\s*", re.M)
_EMOJI = re.compile("[\U0001f300-\U0001faff☀-➿️‍]")
_TERMINAL = ".!?…»\"')]"


def clean_text(raw: str) -> str:
    """Make *raw* something worth reading aloud, keeping every word of the prose.

    Drops what a voice would stumble over — markdown marks, links' URLs, code blocks,
    emoji — and makes sure each paragraph and heading ends in a full stop so the model
    pauses where the author did (a heading with no punctuation is otherwise glued onto the
    next sentence). Never rewrites or summarises: the words are the user's.
    """
    text = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _CODE_FENCE.sub(" ", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _URL.sub("", text)
    text = _HEADING.sub("", text)
    text = _LINE_MARK.sub("", text)
    text = _EMPHASIS.sub("", text)
    text = _EMOJI.sub("", text)

    paragraphs: list[str] = []
    for block in re.split(r"\n\s*\n", text):
        flat = " ".join(block.split())
        if not flat:
            continue
        if flat[-1] not in _TERMINAL and flat[-1].isalnum():
            flat += "."
        paragraphs.append(flat)
    return "\n\n".join(paragraphs)


def decode_text_file(data: bytes) -> str:
    """Text from the bytes of a ``.txt`` / ``.md`` file, whatever code page saved it.

    A Russian text saved by an older Windows editor is cp1251, a French one cp1252; read
    as UTF-8 they raise — or, worse, with ``errors="replace"`` they turn into a chapter of
    replacement characters (U+FFFD) that a voice then reads aloud. So: BOM'd UTF-16, then strict UTF-8, then cp1251 if
    the result is clearly Cyrillic, else cp1252. Never raises.
    """
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    as_1251 = data.decode("cp1251", errors="replace")
    letters = [c for c in as_1251 if c.isalpha()]
    if letters and sum(1 for c in letters if "Ѐ" <= c <= "ӿ") / len(letters) > 0.5:
        return as_1251
    return data.decode("cp1252", errors="replace")


_FR_MARKERS = (" le ", " la ", " les ", " des ", " une ", " est ", " et ", " que ", " qui ",
               " pas ", " pour ", " dans ", " je ", " vous ", " nous ", " avec ", " sur ")
_EN_MARKERS = (" the ", " and ", " of ", " to ", " is ", " that ", " it ", " with ", " for ",
               " was ", " you ", " his ", " her ", " they ")


def detect_language(text: str) -> str | None:
    """``ru`` | ``fr`` | ``en``, or ``None`` when it cannot tell.

    Cyrillic is unambiguous. Latin is told apart by function words. ``None`` is a real
    answer — a Spanish or German text would otherwise be handed a wrong ``language_code``
    and mispronounced; with ``None`` the multilingual model decides for itself.
    """
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return None
    if sum(1 for c in letters if "Ѐ" <= c <= "ӿ") / len(letters) > 0.3:
        return "ru"
    flat = " " + re.sub(r"[^\w']+", " ", text.lower()) + " "
    fr = sum(flat.count(w) for w in _FR_MARKERS)
    en = sum(flat.count(w) for w in _EN_MARKERS)
    fr += sum(1 for c in letters if c in "éèêàùçôîïâëœ") * 0.25
    if fr >= 3 and fr > en:
        return "fr"
    if en >= 3 and en > fr:
        return "en"
    return None


# ── planning ──────────────────────────────────────────────────────────────────


def _link_context(pieces: list[str]) -> tuple[Chunk, ...]:
    chunks: list[Chunk] = []
    for i, piece in enumerate(pieces):
        before = pieces[i - 1][-_CONTEXT_CHARS:] if i > 0 else None
        after = pieces[i + 1][:_CONTEXT_CHARS] if i + 1 < len(pieces) else None
        chunks.append(
            Chunk(index=i, speaker="reader", text=piece, previous_text=before, next_text=after)
        )
    return tuple(chunks)


def _group_into_parts(
    chunks: tuple[Chunk, ...], part_chars: int
) -> tuple[tuple[Chunk, ...], ...]:
    """Pack chunks into parts of about ``part_chars``. A chunk is never split."""
    parts: list[list[Chunk]] = []
    size = 0
    for chunk in chunks:
        if parts and size + chunk.chars > part_chars:
            parts.append([])
            size = 0
        if not parts:
            parts.append([])
        parts[-1].append(chunk)
        size += chunk.chars
    return tuple(tuple(p) for p in parts)


def plan_reading(text: str, options: ReadOptions | None = None) -> ReadPlan:
    """Plan a reading: cleaned text, chunks with context, parts, and the exact cost."""
    options = options or ReadOptions()
    clean = clean_text(text)
    if not clean:
        raise ReadError("there is no readable text — send the text after /read, or a .txt file")
    pieces = split_text(clean, options.max_chunk_chars)
    chunks = _link_context(pieces)
    part_chars = max(int(options.part_minutes * CHARS_PER_MINUTE * options.speed), 1)
    return ReadPlan(
        text=clean,
        lang=options.lang or detect_language(clean),
        model=options.model,
        speed=options.speed,
        chunks=chunks,
        parts=_group_into_parts(chunks, part_chars),
    )


def options_from_config(**overrides: Any) -> ReadOptions:
    """:class:`ReadOptions` from ``audio.read.*`` in config, then *overrides* (None = unset).

    Config is read through core when it is there and skipped when it is not (standalone
    plugin), so the engine itself never imports core. ``navig config set a b`` stores
    STRINGS, hence the explicit float conversions.
    """
    values: dict[str, Any] = {}
    try:
        from navig.config import get as config_get

        for key in ("voice", "lang", "speed", "stability", "part_minutes"):
            raw = config_get(f"audio.read.{key}", None)
            if raw not in (None, ""):
                values[key] = raw
    except Exception:  # noqa: BLE001 — no core / unreadable config: engine defaults apply
        values = {}
    for key, raw in overrides.items():
        if raw is not None:
            values[key] = raw
    try:
        for key in ("speed", "stability", "part_minutes"):
            if key in values:
                values[key] = float(values[key])
        if "speed" in values:
            values["speed"] = min(max(values["speed"], 0.7), 1.2)  # the provider's range
        if "part_minutes" in values:
            values["part_minutes"] = max(values["part_minutes"], 1.0)
    except (TypeError, ValueError) as exc:
        raise ReadError(f"audio.read.* has a value that is not a number: {exc}") from exc
    return ReadOptions(**values)


# ── synthesis ─────────────────────────────────────────────────────────────────


async def resolve_voice(voice: str | None, gen: Any) -> str:
    """A voice id from an id, the NAME of one of your voices, or the configured default."""
    if not voice:
        return str(gen.config.tts_voice_id)
    if _VOICE_ID.match(voice):
        return voice
    from navig_audio.podcast.voices import list_voices

    wanted = voice.strip().lower()
    for candidate in await list_voices():
        if candidate.name.lower() == wanted:
            return candidate.voice_id
    raise ReadError(f"no voice named {voice!r} — list yours with `navig audio voices`")


def _clip_key(chunk: Chunk, plan: ReadPlan, voice_id: str, output_format: str, settings: dict[str, Any]) -> str:
    return cache.clip_key(
        text=chunk.text,
        voice_id=voice_id,
        model=plan.model,
        output_format=output_format,
        settings=settings,
        previous_text=chunk.previous_text,
        next_text=chunk.next_text,
    )


def estimate_spend(plan: ReadPlan, options: ReadOptions, voice_id: str | None = None) -> int:
    """Credits this reading would actually CHARGE: cached chunks are free.

    ``plan.credits`` is the price of the text from scratch; this is what is left to pay
    after the clip cache — so re-sending a chapter you already had read says 0, not
    40,000. With no ``voice_id`` (a voice given by name needs a network lookup to
    resolve) nothing can be proven cached and the full price is returned: an upper bound
    is the safe thing to quote before spending.
    """
    if not voice_id:
        return plan.credits
    output_format = AudioGenerationConfig.from_env().output_format
    settings = _settings(options)
    fresh = sum(
        chunk.chars
        for chunk in plan.chunks
        if not cache.locate(_clip_key(chunk, plan, voice_id, output_format, settings)).exists
    )
    return round(fresh * credit_rate(plan.model))


def _settings(options: ReadOptions) -> dict[str, Any]:
    out: dict[str, Any] = {
        "stability": options.stability,
        "similarity_boost": 0.75,
        "style": 0.0,
        "use_speaker_boost": True,
    }
    if abs(options.speed - 1.0) > 0.01:
        out["speed"] = options.speed
    return out


async def read_aloud(
    plan: ReadPlan,
    options: ReadOptions,
    out_dir: Path,
    *,
    deliver: Deliver | None = None,
    progress: Progress | None = None,
    cancel: asyncio.Event | None = None,
    generator: Any = None,
) -> ReadSummary:
    """Synthesise *plan* into ``out_dir`` as numbered parts, handing each to ``deliver``.

    ``deliver`` is awaited the moment a part exists, **before** the next part starts
    generating — that ordering is the feature (the first words arrive while the rest is
    still being made). A failed delivery is counted, not fatal: a later part may still
    reach the listener, and the files are on disk either way.

    Failures land on ``summary.error`` and never discard work: the parts already made are
    real and already delivered. That includes a voice that does not exist and a provider
    that errors on chunk 1 — the caller reads ``summary.error`` / ``summary.parts``.
    :class:`ReadError` is raised only for a missing ffmpeg, which is a property of the
    machine and must stop before any credit is spent.
    """
    say: Progress = progress or (lambda _msg: None)
    if not ffmpeg_available():
        raise ReadError(
            "ffmpeg is not installed or not on PATH — it joins the clips "
            "(scoop install ffmpeg / brew install ffmpeg / apt install ffmpeg)"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = ReadSummary(out_dir=out_dir, rate=credit_rate(plan.model))

    owns_generator = generator is None
    if owns_generator:
        config = AudioGenerationConfig.from_env()
        config.tts_model = plan.model
        generator = AudioGenerator(config)

    try:
        voice_id = await resolve_voice(options.voice, generator)
        output_format = generator.config.output_format
        settings = _settings(options)
        request_ids: list[str] = []

        for number, group in enumerate(plan.parts, start=1):
            if cancel is not None and cancel.is_set():
                summary.cancelled = True
                break
            clips: list[Path] = []
            for chunk in group:
                if cancel is not None and cancel.is_set():
                    summary.cancelled = True
                    break
                key = _clip_key(chunk, plan, voice_id, output_format, settings)
                clip = cache.locate(key)
                if clip.exists:
                    summary.cached_chunks += 1
                else:
                    say(f"  part {number}/{plan.part_count} · generating {chunk.chars} chars")
                    timed = await generator.tts_with_timestamps(
                        chunk.text,
                        voice_id,
                        model_id=plan.model,
                        voice_settings=settings,
                        language_code=plan.lang,
                        previous_text=chunk.previous_text,
                        next_text=chunk.next_text,
                        previous_request_ids=request_ids[-3:] or None,
                    )
                    if not timed.audio:
                        raise ReadError(
                            f"the provider returned no audio for chunk {chunk.index} "
                            f"({chunk.chars} chars)"
                        )
                    cache.store(key, timed.audio)
                    clip = cache.locate(key)
                    summary.generated_chunks += 1
                    summary.generated_chars += chunk.chars
                    if getattr(timed, "request_id", None):
                        request_ids.append(timed.request_id)
                clips.append(clip.audio)
            if summary.cancelled:
                break

            stem = f"{plan.slug}-part-{number:02d}-of-{plan.part_count:02d}"
            joined = out_dir / f".{stem}.joined.mp3"
            final = out_dir / f"{stem}.mp3"
            await asyncio.to_thread(concat, clips, joined, gap_s=SILENCE_BETWEEN_CHUNKS_S)
            joined_s = await asyncio.to_thread(probe_duration, joined)
            await asyncio.to_thread(normalize, joined, final, i=options.loudness_lufs)
            duration = await asyncio.to_thread(probe_duration, final)
            if duration < joined_s * NORMALIZE_MIN_KEPT:
                # ffmpeg's loudnorm can silently CUT audio (measured: a 1.9 s join with
                # silence gaps came back 0.8 s). A shorter file is a missing stretch of
                # someone's book, so trust the un-normalised join instead — slightly less
                # even in volume beats a hole in the story — and say so.
                summary.warnings.append(
                    f"part {number}: loudness normalisation shortened the audio "
                    f"({joined_s:.1f}s -> {duration:.1f}s); kept the un-normalised version"
                )
                say(f"  {summary.warnings[-1]}")
                joined.replace(final)
                duration = joined_s
            else:
                joined.unlink(missing_ok=True)

            part = ReadPart(
                index=number,
                total=plan.part_count,
                path=final,
                duration_s=duration,
                chars=sum(c.chars for c in group),
            )
            summary.parts.append(part)
            if deliver is not None:
                try:
                    delivered = await deliver(part)
                except Exception as exc:  # noqa: BLE001 — the files exist; keep reading
                    say(f"  delivery of part {number} failed: {exc}")
                    delivered = False
                if not delivered:
                    summary.delivery_failures += 1
    except asyncio.CancelledError:
        summary.cancelled = True
        raise
    except (ReadError, AudioEditError) as exc:
        summary.error = str(exc)
    except Exception as exc:  # noqa: BLE001 — provider/network: keep the parts already made
        summary.error = f"{type(exc).__name__}: {exc}"
    finally:
        if owns_generator:
            await generator.close()
    return summary
