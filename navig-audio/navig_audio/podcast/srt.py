"""Subtitles, built from the timings of the audio that actually shipped.

The generation call returns a start and end time for every character it spoke. That is
far finer than a subtitle needs, so the work here is grouping: turn a stream of
character timings into readable lines that appear and disappear with the voice.

Timings are per request, and a track is many requests played back to back, so each
chunk's times have to be shifted by everything before it. Getting that offset wrong is
the classic subtitle failure — the first line looks perfect and everything after it
drifts further out of sync.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Two lines of comfortable reading. Longer cues force viewers to pause; shorter ones
# flicker.
MAX_CUE_CHARS = 84
# Below this, a sentence end is not worth breaking on — it would produce a cue that
# flashes past before it can be read.
MIN_CUE_CHARS = 24

_SENTENCE_END = re.compile(r"[.!?…]")


@dataclass(frozen=True)
class SubtitleCue:
    index: int
    start: float
    end: float
    text: str

    @property
    def duration(self) -> float:
        return max(self.end - self.start, 0.0)


def format_timestamp(seconds: float) -> str:
    """``HH:MM:SS,mmm`` — the SRT time format, comma decimal separator included."""
    if seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def cues_from_alignment(
    characters: list[str],
    starts: list[float],
    ends: list[float],
    *,
    offset: float = 0.0,
    start_index: int = 1,
    max_chars: int = MAX_CUE_CHARS,
) -> list[SubtitleCue]:
    """Group character timings into readable subtitle cues.

    ``offset`` shifts every time by where this chunk sits in the finished track.
    """
    if not characters:
        return []
    # Ragged arrays would silently mis-time everything after the short one; truncating
    # to the common length keeps the rest of the track correct.
    usable = min(len(characters), len(starts), len(ends))

    cues: list[SubtitleCue] = []
    index = start_index
    buffer: list[str] = []
    cue_start: float | None = None
    last_break: int | None = None  # position in `buffer` just after the last space

    def flush(end_time: float) -> None:
        nonlocal buffer, cue_start, index, last_break
        text = "".join(buffer).strip()
        if text and cue_start is not None:
            cues.append(
                SubtitleCue(
                    index=index,
                    start=cue_start + offset,
                    end=end_time + offset,
                    text=text,
                )
            )
            index += 1
        buffer = []
        cue_start = None
        last_break = None

    for position in range(usable):
        char = characters[position]
        if cue_start is None:
            if not char.strip():
                continue  # never start a cue on leading whitespace
            cue_start = starts[position]
        buffer.append(char)
        if char == " ":
            last_break = len(buffer)

        length = len("".join(buffer).strip())
        if _SENTENCE_END.match(char) and length >= MIN_CUE_CHARS:
            flush(ends[position])
        elif length >= max_chars:
            if last_break and last_break < len(buffer):
                # Break at the last space so a word is never split across two cues.
                carry = buffer[last_break:]
                held = buffer[:last_break]
                buffer = held
                flush(ends[position - len(carry)])
                buffer = carry
                cue_start = starts[position - len(carry) + 1] if carry else None
                last_break = None
            else:
                flush(ends[position])

    if buffer:
        flush(ends[usable - 1])
    return cues


def to_srt(cues: list[SubtitleCue]) -> str:
    """Serialise cues as an SRT document."""
    blocks = []
    for cue in cues:
        blocks.append(
            f"{cue.index}\n"
            f"{format_timestamp(cue.start)} --> {format_timestamp(cue.end)}\n"
            f"{cue.text}\n"
        )
    return "\n".join(blocks)


def renumber(cues: list[SubtitleCue], start: int = 1) -> list[SubtitleCue]:
    """Re-index cues consecutively — needed after concatenating tracks into a master."""
    return [
        SubtitleCue(index=start + offset, start=c.start, end=c.end, text=c.text)
        for offset, c in enumerate(cues)
    ]


def shift(cues: list[SubtitleCue], seconds: float) -> list[SubtitleCue]:
    """Move every cue along the timeline by ``seconds``."""
    return [
        SubtitleCue(index=c.index, start=c.start + seconds, end=c.end + seconds, text=c.text)
        for c in cues
    ]
