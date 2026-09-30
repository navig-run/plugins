"""Splitting a track into requests the model can actually take — without losing the take.

Two constraints collide here. The model has a hard per-request character limit, so a
long track has to be split. But every request re-decides its own delivery, so a naive
split produces speech that audibly restarts: clip one builds momentum, clip two begins
flat, and the seam is obvious to anyone listening.

The fix is not to split less, it is to tell each request what surrounds it.
``previous_text`` and ``next_text`` give the model the neighbouring words as context it
should sound continuous with but must not speak, and ``previous_request_ids`` points at
the actual generations before it. Chunks are cut on sentence boundaries so the context
handed over is a whole thought rather than half a clause.

Context is only carried **within a run of one speaker**. Conditioning one voice on
another's words is not continuity, it is contamination — a reply should not inherit the
cadence of the question.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from navig_audio.podcast.scenario import Cue, Track

# Well under the model's 10k ceiling. The limit that matters in practice is not the
# API's but the ear's: very long single generations drift in tone, and a failed request
# costs the whole chunk, so smaller chunks also mean cheaper retries.
DEFAULT_MAX_CHARS = 2500

# How much neighbouring text to hand over as context. Enough to establish cadence,
# short enough that it does not dominate the request.
CONTEXT_CHARS = 400

# Sentence ends, including the French quotation and spacing conventions these scripts use.
_SENTENCE_END = re.compile(r"(?<=[.!?…])[\"'»)\]]*\s+")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Chunk:
    """One TTS request: what to say, in whose voice, and what surrounds it."""

    index: int
    speaker: str
    text: str
    previous_text: str | None = None
    next_text: str | None = None

    @property
    def chars(self) -> int:
        return len(self.text)


def split_text(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> list[str]:
    """Split ``text`` into pieces of at most ``max_chars``, preferring sentence ends.

    Falls back to word boundaries, then to a hard cut, because a sentence longer than
    the limit is rare but must not raise — an un-renderable episode is a worse outcome
    than one awkward seam.
    """
    text = _WHITESPACE.sub(" ", text).strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    pieces: list[str] = []
    for sentence in _split_sentences(text):
        if len(sentence) <= max_chars:
            _append_packed(pieces, sentence, max_chars)
            continue
        for word_piece in _split_words(sentence, max_chars):
            _append_packed(pieces, word_piece, max_chars)
    return [p for p in pieces if p]


def _split_sentences(text: str) -> list[str]:
    parts = _SENTENCE_END.split(text)
    return [p.strip() for p in parts if p.strip()]


def _split_words(sentence: str, max_chars: int) -> list[str]:
    """Break an over-long sentence on spaces, hard-cutting only if a word itself is huge."""
    out: list[str] = []
    current = ""
    for word in sentence.split(" "):
        while len(word) > max_chars:  # a URL or an unbroken run of characters
            out.append(word[:max_chars])
            word = word[max_chars:]
        candidate = f"{current} {word}".strip()
        if len(candidate) > max_chars:
            if current:
                out.append(current)
            current = word
        else:
            current = candidate
    if current:
        out.append(current)
    return out


def _append_packed(pieces: list[str], addition: str, max_chars: int) -> None:
    """Add ``addition``, merging it into the previous piece when it still fits.

    Without this, every sentence becomes its own request: correct, but far more
    generations than necessary and a seam after every full stop.
    """
    if pieces and len(pieces[-1]) + 1 + len(addition) <= max_chars:
        pieces[-1] = f"{pieces[-1]} {addition}"
    else:
        pieces.append(addition)


def _tail(text: str, limit: int = CONTEXT_CHARS) -> str:
    return text[-limit:] if len(text) > limit else text


def _head(text: str, limit: int = CONTEXT_CHARS) -> str:
    return text[:limit] if len(text) > limit else text


def plan_track(track: Track, max_chars: int = DEFAULT_MAX_CHARS) -> list[Chunk | Cue]:
    """The ordered render plan for one track: chunks and cues, in reading order.

    Cues stay in place so the track assembles as written, and they also act as natural
    context boundaries — speech either side of a music sting is not continuous, so
    pretending otherwise would make the model lean into a transition that is not there.
    """
    plan: list[Chunk | Cue] = []
    index = 0

    for segment in track.segments:
        if isinstance(segment, Cue):
            plan.append(segment)
            continue
        for piece in split_text(segment.text, max_chars):
            plan.append(Chunk(index=index, speaker=segment.speaker, text=piece))
            index += 1

    return _link_context(plan)


def _link_context(plan: list[Chunk | Cue]) -> list[Chunk | Cue]:
    """Fill in ``previous_text`` / ``next_text`` across same-speaker runs."""
    linked: list[Chunk | Cue] = list(plan)
    for position, item in enumerate(linked):
        if not isinstance(item, Chunk):
            continue
        previous = _neighbour(linked, position, step=-1, speaker=item.speaker)
        following = _neighbour(linked, position, step=+1, speaker=item.speaker)
        linked[position] = Chunk(
            index=item.index,
            speaker=item.speaker,
            text=item.text,
            previous_text=_tail(previous) if previous else None,
            next_text=_head(following) if following else None,
        )
    return linked


def _neighbour(
    plan: list[Chunk | Cue], position: int, *, step: int, speaker: str
) -> str | None:
    """The adjacent chunk's text, if it belongs to the same uninterrupted speaker run."""
    neighbour = position + step
    if not 0 <= neighbour < len(plan):
        return None
    item = plan[neighbour]
    if not isinstance(item, Chunk) or item.speaker != speaker:
        return None
    return item.text


def chunk_count(track: Track, max_chars: int = DEFAULT_MAX_CHARS) -> int:
    """How many TTS requests this track will take."""
    return sum(1 for item in plan_track(track, max_chars) if isinstance(item, Chunk))
