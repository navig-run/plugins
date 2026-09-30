"""Captions — the words of a track, and *when* each one lands.

For short-form video the timings are the product, not the text: they are the ``.srt`` that
gets burned in, and how a passage of a long track is found at all. navig-audio transcribes
with word timings (:func:`navig_audio.voice.words.transcribe_words` — the OpenAI Whisper API
with a key, faster-whisper on this machine without one); this module turns that answer into
captions: lines a phone can read, chunking for long tracks, the hallucination filter, and
two shapes of the result — a :class:`Transcript` for code, and an SRT for ffmpeg.

**Language is detected, never assumed.** Forcing ``ru`` on a French track does not produce a
bad transcript, it produces the *placeholder* "PESNYA NA FRANTSUZSKOM YAZYKE" — a sentence
that looks like a transcript and is not one. Pass ``language`` only when it is known.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

# The Whisper API's 25 MB cap is checked by navig-audio (navig_audio.voice.words), which
# owns the transcription call; local faster-whisper has no cap.

# Whisper internally works in 30-second windows, and on dense material -- a rap vocal
# sitting inside a loud mix -- it returns less for a long file than for the same audio
# submitted in pieces. Measured on an 82-second track: 86 words whole, 111 chunked, and
# the chunked pass was the only one that found the outro's repeated line.
#
# That is a bad shape of failure: the call succeeds, a transcript comes back, and the only
# clue that a passage is missing is a silence in the timings that looks exactly like an
# instrumental break. So anything longer than CHUNK_S is chunked by default.
#
# What chunking does NOT do is make an untranscribable passage transcribable. On that same
# track a twenty-second stretch has clear voice-band energy and yet returned a DIFFERENT
# answer from every window tried -- "Rhymes with a G O A T", "Like and Subscribe", "Outro".
# Text that changes with the window is not a transcript, it is the model guessing, and a
# caller who treats it as lyrics will burn a hallucinated line into a video. When in doubt,
# compare two windows: agreement is evidence, disagreement means there is nothing there to
# read.
CHUNK_S = 30.0
# Overlap so a word spanning a boundary is heard whole by at least one chunk; the merge
# then drops what it has already seen.
CHUNK_OVERLAP_S = 5.0

# Line-breaking. A caption longer than this is unreadable on a phone held at arm's length,
# and a gap longer than that is a new thought rather than a continuation.
MAX_LINE_CHARS = 42
LINE_GAP_S = 0.65
MAX_LINE_S = 5.0

# Whisper hallucinates subtitle-house credits over music and applause — it has seen a great
# many fan-subbed videos whose last line is exactly this. It fired on sabdoza-04a and would
# have been burned into the picture. These are dropped and the drop is REPORTED, because a
# filter that silently eats a real lyric is worse than the hallucination it prevents.
HALLUCINATIONS = (
    r"^\s*(\u0440\u0435\u0434\u0430\u043a\u0442\u043e\u0440\s+\u0441\u0443\u0431\u0442\u0438\u0442\u0440\u043e\u0432|\u0441\u0443\u0431\u0442\u0438\u0442\u0440\u044b\s+(\u0441\u0434\u0435\u043b\u0430\u043b|\u0434\u0435\u043b\u0430\u043b)|"
    r"\u043a\u043e\u0440\u0440\u0435\u043a\u0442\u043e\u0440|\u0441\u0443\u0431\u0442\u0438\u0442\u0440\u044b\s+\u0438\s+\u043f\u0435\u0440\u0435\u0432\u043e\u0434)\b",
    r"^\s*(subtitles?\s+by|amara\.org|subscribe|thanks?\s+for\s+watching)\b",
    r"^\s*(sous-titr\w+\s+(par|r\u00e9alis\u00e9s))\b",
    r"^\s*\u043f\u0440\u043e\u0434\u043e\u043b\u0436\u0435\u043d\u0438\u0435\s+\u0441\u043b\u0435\u0434\u0443\u0435\u0442\b",
)
_HALLUCINATION_RE = tuple(re.compile(p, re.IGNORECASE) for p in HALLUCINATIONS)


class CaptionError(RuntimeError):
    """The track could not be transcribed."""


@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float


@dataclass
class Line:
    text: str
    start: float
    end: float
    words: list[Word] = field(default_factory=list)


@dataclass
class Transcript:
    """What was said, when — plus what was thrown away and why."""

    language: str
    duration: float
    words: list[Word]
    lines: list[Line]
    text: str
    dropped: list[str] = field(default_factory=list)

    @property
    def word_count(self) -> int:
        return len(self.words)


def _is_hallucination(text: str) -> bool:
    return any(rx.search(text) for rx in _HALLUCINATION_RE)


def group_words(words: list[Word]) -> list[Line]:
    """Break a word stream into caption lines.

    A line ends at sentence-ending punctuation, at a pause, when it would grow past
    :data:`MAX_LINE_CHARS`, or when it has been on screen for :data:`MAX_LINE_S`. Splitting
    on *time* as well as length matters for a song: a slow, sparse hook can be six short
    words spread over eight seconds, and a caption that hangs that long reads as frozen.
    """
    lines: list[Line] = []
    current: list[Word] = []

    def flush() -> None:
        if not current:
            return
        text = " ".join(w.text for w in current).strip()
        if text:
            lines.append(Line(text=text, start=current[0].start, end=current[-1].end,
                              words=list(current)))
        current.clear()

    for i, word in enumerate(words):
        current.append(word)
        nxt = words[i + 1] if i + 1 < len(words) else None
        width = sum(len(w.text) + 1 for w in current) - 1
        span = current[-1].end - current[0].start
        gap = (nxt.start - word.end) if nxt else 0.0
        ends_sentence = word.text.rstrip().endswith((".", "!", "?", ":", ";"))
        if nxt is None or ends_sentence or gap >= LINE_GAP_S or width >= MAX_LINE_CHARS \
                or span >= MAX_LINE_S:
            flush()
    flush()
    return lines


def to_srt(lines: list[Line], *, offset: float = 0.0) -> str:
    """Render caption lines as SubRip.

    ``offset`` shifts every timestamp — what a passage cut out of a longer track needs, so
    the words of an excerpt line up with the excerpt rather than with the original.
    """
    def stamp(t: float) -> str:
        t = max(0.0, t)
        h, rem = divmod(t, 3600)
        m, s = divmod(rem, 60)
        return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int(round((s % 1) * 1000)):03d}"

    out: list[str] = []
    for n, line in enumerate(lines, start=1):
        out.append(str(n))
        out.append(f"{stamp(line.start - offset)} --> {stamp(line.end - offset)}")
        out.append(line.text)
        out.append("")
    return "\n".join(out)


def slice_lines(lines: list[Line], start: float, end: float) -> list[Line]:
    """The caption lines that fall inside ``[start, end)``.

    A line is kept when it *overlaps* the window, not only when it is wholly inside it: a
    passage that begins mid-line still needs that line on screen, otherwise the clip opens
    on a voice with no words.
    """
    return [ln for ln in lines if ln.end > start and ln.start < end]


def parse_response(payload: dict[str, Any]) -> Transcript:
    """Turn Whisper's ``verbose_json`` into a :class:`Transcript`.

    Split out from the network call so the shape can be tested without an API key — the
    grouping and the hallucination filter are where the behaviour actually lives.
    """
    raw_words = payload.get("words") or []
    words: list[Word] = []
    for item in raw_words:
        text = str(item.get("word", "")).strip()
        if not text:
            continue
        words.append(Word(text=text, start=float(item.get("start", 0.0)),
                          end=float(item.get("end", 0.0))))

    if not words:
        # No word timings: fall back to the segments, which every model returns. The
        # captions are coarser but present, which beats an empty file.
        for seg in payload.get("segments") or []:
            text = str(seg.get("text", "")).strip()
            if text:
                words.append(Word(text=text, start=float(seg.get("start", 0.0)),
                                  end=float(seg.get("end", 0.0))))

    lines = group_words(words)
    kept: list[Line] = []
    dropped: list[Line] = []
    for line in lines:
        (dropped if _is_hallucination(line.text) else kept).append(line)

    return Transcript(
        language=str(payload.get("language") or "unknown"),
        duration=float(payload.get("duration") or (words[-1].end if words else 0.0)),
        words=words,
        lines=kept,
        text=str(payload.get("text") or " ".join(w.text for w in words)).strip(),
        dropped=[ln.text for ln in dropped],
    )


def transcribe_one(audio: Path, *, language: str | None = None,
                   timeout: int = 600, engine: str | None = None) -> Transcript:
    """Transcribe one file in a single request — no chunking.

    Raises :class:`CaptionError` rather than returning an empty transcript: a caller that
    gets back "no words" cannot tell a silent track from a missing API key, and only one of
    those is fixable.
    """
    # navig-audio owns speech-to-text: the OpenAI Whisper API with word timings when a key
    # resolves, faster-whisper on this machine otherwise — so captions need no key at all.
    from navig_audio.voice.words import WordsError, transcribe_words

    try:
        payload = transcribe_words(audio, language=language, engine=engine, timeout=timeout)
    except WordsError as exc:
        raise CaptionError(str(exc)) from exc

    transcript = parse_response(payload)
    if not transcript.words:
        raise CaptionError(
            f"{audio.name} transcribed to nothing - an instrumental, or the wrong file?"
        )
    return transcript


def merge_transcripts(parts: list[tuple[float, Transcript]]) -> Transcript:
    """Stitch chunk transcripts into one, dropping what the overlap heard twice.

    ``parts`` is ``(offset_seconds, transcript)`` in order. Each chunk's timings are
    rebased onto the whole file, then a word is kept only if it starts after everything
    already accepted — which is what removes the duplicate of an overlapping region
    without needing to know where the boundary was.
    """
    words: list[Word] = []
    dropped: list[str] = []
    language = "unknown"
    duration = 0.0
    for offset, part in parts:
        if part.language and part.language != "unknown" and language == "unknown":
            language = part.language
        duration = max(duration, offset + part.duration)
        dropped.extend(part.dropped)
        for w in part.words:
            start = w.start + offset
            # A tolerance rather than a strict >: the same word heard by two chunks lands
            # a few tens of milliseconds apart, and a strict test would keep both.
            if words and start < words[-1].end - 0.05:
                continue
            words.append(Word(text=w.text, start=start, end=w.end + offset))

    lines = group_words(words)
    kept = [ln for ln in lines if not _is_hallucination(ln.text)]
    dropped.extend(ln.text for ln in lines if _is_hallucination(ln.text))
    return Transcript(
        language=language, duration=duration, words=words, lines=kept,
        text=" ".join(w.text for w in words).strip(), dropped=dropped,
    )


def chunk_bounds(duration: float, chunk_s: float = CHUNK_S,
                 overlap_s: float = CHUNK_OVERLAP_S) -> list[tuple[float, float]]:
    """Where to cut a long file, as ``(start, end)`` windows that overlap.

    The final window is pulled back to end exactly at ``duration`` rather than being left
    as a two-second sliver: a very short chunk gives Whisper too little context and is
    where a dropped last line comes from.
    """
    if duration <= chunk_s:
        return [(0.0, duration)]
    step = max(1.0, chunk_s - overlap_s)
    bounds: list[tuple[float, float]] = []
    start = 0.0
    while start < duration:
        end = min(start + chunk_s, duration)
        bounds.append((start, end))
        if end >= duration:
            break
        start += step
    # Absorb a final sliver into its predecessor.
    if len(bounds) > 1 and bounds[-1][1] - bounds[-1][0] < chunk_s / 3:
        last = bounds.pop()
        bounds[-1] = (bounds[-1][0], last[1])
    return bounds


def transcribe(audio: Path, *, language: str | None = None, chunked: bool | None = None,
               timeout: int = 600,
               progress: Any = None, engine: str | None = None) -> Transcript:
    """Transcribe ``audio`` with word-level timings, in overlapping chunks when it is long.

    ``engine`` is navig-audio's: ``"whisper_api"`` (an OpenAI key), ``"whisper_local"``
    (faster-whisper, no key), or ``None`` for the API when a key resolves and local otherwise.

    ``chunked`` defaults to "yes if the file is longer than :data:`CHUNK_S`". Pass ``False``
    to force one request — cheaper, and correct for clean speech — or ``True`` to force
    chunking on a short file.

    The extra requests cost fractions of a cent. Losing the chorus costs the video.
    """
    from navig_generate.media.audio_edit import probe_duration

    audio = Path(audio)
    if not audio.exists():
        raise CaptionError(f"audio not found: {audio}")

    try:
        duration = probe_duration(audio)
    except Exception:  # noqa: BLE001 - a file ffprobe cannot read still deserves one try
        duration = 0.0

    if chunked is None:
        chunked = duration > CHUNK_S
    if not chunked or duration <= 0:
        return transcribe_one(audio, language=language, timeout=timeout, engine=engine)

    import tempfile

    from navig_generate.media.audio_edit import excerpt

    bounds = chunk_bounds(duration)
    if len(bounds) == 1:
        return transcribe_one(audio, language=language, timeout=timeout, engine=engine)

    parts: list[tuple[float, Transcript]] = []
    with tempfile.TemporaryDirectory(prefix="navig-captions-") as tmp:
        for n, (start, end) in enumerate(bounds, start=1):
            if progress:
                progress(f"  chunk {n}/{len(bounds)}: {start:.0f}s-{end:.0f}s")
            piece = Path(tmp) / f"chunk-{n:02d}.wav"
            excerpt(audio, piece, start, end)
            try:
                parts.append((start, transcribe_one(piece, language=language, timeout=timeout, engine=engine)))
            except CaptionError as exc:
                # One deaf chunk must not lose the rest of the song. Report and continue —
                # a transcript with a hole in it, clearly announced, beats no transcript.
                _log.warning("chunk %d (%.0fs-%.0fs) failed: %s", n, start, end, exc)
                if progress:
                    progress(f"  ! chunk {n} failed ({exc}) - continuing without it")

    if not parts:
        raise CaptionError(f"{audio.name} transcribed to nothing in any chunk")
    merged = merge_transcripts(parts)
    if not merged.words:
        raise CaptionError(
            f"{audio.name} transcribed to nothing - an instrumental, or the wrong file?"
        )
    return merged
