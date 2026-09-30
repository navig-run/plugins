"""Hooks — which passage of a track is worth cutting a clip from.

A two-minute song makes several short videos, and picking the passages by ear means
listening to it twenty times. This ranks them instead.

**It is a shortlist, not an oracle.** Nothing here can know what an audience will do. What
it can do is measure four things that a hook demonstrably has, report each one separately
so the ranking can be argued with, and put the words on screen next to the timecode so the
choice is made by a person looking at lyrics rather than at a number.

The four signals, and why each is defensible:

``repeat``
    How much of the passage's wording recurs elsewhere in the track. In music the chorus is
    *by construction* the part that repeats — this is the one signal that is not a proxy for
    something else, and it is weighted highest for that reason.
``density``
    Words per second. A passage with no words is atmosphere; it may be beautiful and it is
    not a hook.
``energy``
    Mean onset strength, from the same detector :mod:`navig.media.beats` already uses. The
    busy, loud part.
``clean``
    Whether the window starts and ends on a phrase boundary rather than mid-word. A clip
    that opens halfway through a line reads as a mistake no matter how good the line is.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

# Short-form windows. Below MIN there is no room for a hook to land; above MAX a viewer
# has already decided. The defaults bracket the length that has actually performed here.
MIN_WINDOW_S = 10.0
MAX_WINDOW_S = 25.0

# A pause this long is a phrase boundary — a place a cut can start or end without
# sounding severed. It is a STARTING point, not a rule: see :func:`phrase_gap`.
PHRASE_GAP_S = 0.45
# ...and this is the floor it will never go under. Below ~0.18s the "pause" is the space
# between syllables, and a cut there sounds like a dropout rather than an edit.
MIN_PHRASE_GAP_S = 0.18

# n-gram width for the repeat signal. Two words is noise ("and the"); four rarely recurs
# verbatim once a singer varies a line. Three is where a chorus actually shows up.
NGRAM = 3

# How much material two candidates may share before the second is just the first with the
# boundaries jiggled. See :func:`_spread`.
MAX_OVERLAP = 0.2

# How the four signals combine. `repeat` leads because it is the only one that measures the
# thing itself rather than a correlate of it.
WEIGHTS = {"repeat": 0.40, "density": 0.25, "energy": 0.25, "clean": 0.10}

_WORD_RE = re.compile(r"\w+", re.UNICODE)


@dataclass
class Passage:
    """One candidate cut, with the reasoning kept attached to it."""

    start: float
    end: float
    text: str
    score: float
    parts: dict[str, float] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return self.end - self.start


def normalise(word: str) -> str:
    return word.lower().strip(".,!?:;\u2026\"'()[]-\u2014\u00ab\u00bb")


def phrase_gap(words: list[Any], want: int, floor: float = MIN_PHRASE_GAP_S) -> float:
    """The pause length that counts as a phrase break *on this track*.

    A fixed threshold does not survive contact with real material. On a densely-sung track
    almost no gap reaches :data:`PHRASE_GAP_S`, so the candidate list collapses to two or
    three windows — and nothing in the output tells you whether that is the song or the
    setting. Measured on b1ch.mp3: at a fixed 0.45s the whole back half of the track
    offered no legal window at all.

    So the threshold adapts. Start at :data:`PHRASE_GAP_S`; if that yields fewer than
    ``want`` breaks, fall back to the ``want``-th largest gap the singer actually left,
    never going below ``floor`` — under which a "pause" is just articulation between
    syllables and cutting there sounds like a dropout.
    """
    gaps = sorted((n.start - p.end for p, n in zip(words, words[1:])), reverse=True)
    gaps = [g for g in gaps if g > 0]
    if not gaps:
        return PHRASE_GAP_S
    if sum(1 for g in gaps if g >= PHRASE_GAP_S) >= want:
        return PHRASE_GAP_S
    return max(floor, gaps[min(want, len(gaps)) - 1])


def phrase_bounds(words: list[Any], gap_s: float | None = None) -> tuple[list[float], list[float]]:
    """Where phrases start and end, from the silences between words.

    Returns ``(starts, ends)``. The first word always starts a phrase and the last always
    ends one — otherwise a track with no internal pause would offer no legal window at all.

    ``gap_s`` defaults to :func:`phrase_gap`, which measures the track rather than assuming
    a number that suits one.
    """
    if not words:
        return [], []
    if gap_s is None:
        span = words[-1].end - words[0].start
        gap_s = phrase_gap(words, want=max(6, int(span / MIN_WINDOW_S) + 2))
    starts = [words[0].start]
    ends: list[float] = []
    for prev, nxt in zip(words, words[1:]):
        if nxt.start - prev.end >= gap_s:
            ends.append(prev.end)
            starts.append(nxt.start)
    ends.append(words[-1].end)
    return starts, ends


def repeat_score(words: list[Any], start: float, end: float) -> float:
    """How much of this window's wording occurs elsewhere in the track.

    Built from :data:`NGRAM`-word shingles: the fraction of the window's shingles that also
    appear outside it. A chorus scores high because it is sung again; a unique verse scores
    near zero, which is correct — a verse heard once is not the part people repeat back.
    """
    def shingles(seq: list[str]) -> set[tuple[str, ...]]:
        return {tuple(seq[i:i + NGRAM]) for i in range(len(seq) - NGRAM + 1)}

    inside = [normalise(w.text) for w in words if start <= w.start < end]
    outside = [normalise(w.text) for w in words if not (start <= w.start < end)]
    inside = [w for w in inside if w]
    outside = [w for w in outside if w]
    mine = shingles(inside)
    if not mine:
        return 0.0
    theirs = shingles(outside)
    return len(mine & theirs) / len(mine)


def energy_score(envelope: Any, times: Any, start: float, end: float) -> float:
    """Mean onset strength inside the window, relative to the track's own peak.

    Relative, not absolute: a quiet track's loud part is still its loud part, and scoring
    it against some fixed dB would rank every gentle song as hookless.
    """
    import numpy as np

    if envelope is None or len(envelope) == 0:
        return 0.0
    mask = (times >= start) & (times < end)
    if not mask.any():
        return 0.0
    peak = float(np.max(envelope)) or 1.0
    return min(1.0, float(np.mean(envelope[mask])) / peak)


def _snap(value: float, grid: list[float], limit: float = 0.35) -> float:
    """Move ``value`` onto the nearest grid point, but only if it is already close.

    A boundary that would have to travel further than ``limit`` is left where the words put
    it: snapping a phrase start half a bar away to make it "musical" cuts off the word the
    passage was chosen for.
    """
    if not grid:
        return value
    nearest = min(grid, key=lambda g: abs(g - value))
    return nearest if abs(nearest - value) <= limit else value


def rank(transcript: Any, audio: Path | None = None, *, top: int = 6,
         min_s: float = MIN_WINDOW_S, max_s: float = MAX_WINDOW_S,
         grid: Any = None) -> list[Passage]:
    """Rank candidate passages of a track, best first.

    Windows are built from phrase boundaries only — every candidate therefore starts and
    ends where the singer did, and the ``clean`` signal distinguishes the ones that also
    land on a *long* pause. Boundaries are then nudged onto the beat where that is a small
    move.

    ``audio`` is optional: without it the energy signal is skipped and the remaining three
    are re-weighted, rather than silently scoring every passage's energy as zero (which
    would flatten the ranking into "whichever has the most repeated words").
    """
    import numpy as np

    words = list(getattr(transcript, "words", []) or [])
    if not words:
        return []

    starts, ends = phrase_bounds(words)
    envelope = times = None
    if audio is not None:
        try:
            from navig_generate.media.beats import HOP, SAMPLE_RATE, decode, onset_envelope

            envelope = onset_envelope(decode(audio))
            times = np.arange(len(envelope)) * (HOP / SAMPLE_RATE)
        except Exception as exc:  # noqa: BLE001 - energy is an enhancement, not a gate
            _log.debug("onset envelope unavailable (%s) - ranking without energy", exc)
            envelope = times = None

    weights = dict(WEIGHTS)
    if envelope is None:
        weights.pop("energy")
        total = sum(weights.values())
        weights = {k: v / total for k, v in weights.items()}

    beats = list(getattr(grid, "downbeats", None) or getattr(grid, "beats", None) or [])
    gaps = {round(s, 3) for s in starts} | {round(e, 3) for e in ends}

    seen: set[tuple[float, float]] = set()
    passages: list[Passage] = []
    for s in starts:
        for e in ends:
            span = e - s
            if not (min_s <= span <= max_s):
                continue
            key = (round(s, 2), round(e, 2))
            if key in seen:
                continue
            seen.add(key)

            inside = [w for w in words if s <= w.start < e]
            if not inside:
                continue
            parts = {
                "repeat": repeat_score(words, s, e),
                "density": min(1.0, (len(inside) / span) / 4.0),
                "clean": (round(s, 3) in gaps) * 0.5 + (round(e, 3) in gaps) * 0.5,
            }
            if envelope is not None:
                parts["energy"] = energy_score(envelope, times, s, e)
            score = sum(parts[k] * w for k, w in weights.items())
            passages.append(Passage(
                start=_snap(s, beats), end=_snap(e, beats),
                text=" ".join(w.text for w in inside).strip(),
                score=score, parts=parts,
            ))

    passages.sort(key=lambda p: p.score, reverse=True)
    return _spread(passages, top)


def _spread(passages: list[Passage], top: int, overlap: float = MAX_OVERLAP) -> list[Passage]:
    """Take the best ``top``, refusing near-duplicates of one already taken.

    Without this the list is the same eight seconds offered eight times with the boundaries
    jiggled — technically the top eight scores, and useless for choosing four clips.

    The threshold is deliberately strict. At a half-window it still returned six candidates
    stepping through the same half-minute, each overlapping its neighbour by 47% and so
    passing individually; the caller wants four clips a viewer would not recognise as the
    same moment, which means a fifth of the material is already too much to share.
    """
    chosen: list[Passage] = []
    for p in passages:
        if any(min(p.end, c.end) - max(p.start, c.start)
               > overlap * min(p.duration, c.duration) for c in chosen):
            continue
        chosen.append(p)
        if len(chosen) >= top:
            break
    return chosen
