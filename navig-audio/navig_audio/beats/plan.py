"""From a style to the request the music model gets.

The composition plan is what makes a generated beat *rappable*: every section has a
duration computed from bars and tempo, so "verse, 16 bars" is sixteen bars and not
"about a minute". The plan is built here, deterministically, from a
:class:`~navig_audio.beats.styles.BeatStyle` and the overrides on the command line —
the provider never has to guess the arrangement.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from navig_audio.beats.styles import INSTRUMENTAL_NEGATIVE, BeatStyle

# The provider's limits on one section (documented: 3 000–120 000 ms).
SECTION_MIN_MS = 3000
SECTION_MAX_MS = 120000
BEATS_PER_BAR = 4

_KEY_RE = re.compile(r"^\s*([A-Ga-g])\s*([#b♭♯]?)\s*(.*?)\s*$")
_MINOR_WORDS = {"m", "min", "minor", "-", "moll"}
_MAJOR_WORDS = {"", "maj", "major", "dur"}
_FLAT_TO_SHARP = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#", "Cb": "B", "Fb": "E"}


def parse_key(text: str) -> tuple[str, str]:
    """``Em`` / ``E minor`` / ``e min`` / ``Bb major`` → ``("E", "minor")``."""
    m = _KEY_RE.match(text or "")
    if not m:
        raise ValueError(f"cannot read a key from {text!r}")
    letter, accidental, rest = m.group(1).upper(), m.group(2), m.group(3).lower()
    if accidental in ("b", "♭"):
        tonic = _FLAT_TO_SHARP.get(letter + "b", letter + "b")
    elif accidental in ("#", "♯"):
        tonic = letter + "#"
    else:
        tonic = letter
    if rest in _MINOR_WORDS:
        mode = "minor"
    elif rest in _MAJOR_WORDS:
        mode = "major"
    else:
        raise ValueError(f"cannot read a key from {text!r}")
    return tonic, mode


def key_name(text: str) -> str:
    tonic, mode = parse_key(text)
    return f"{tonic} {mode}"


def key_short(text: str) -> str:
    tonic, mode = parse_key(text)
    return tonic + ("m" if mode == "minor" else "")


def parse_structure(text: str) -> list[tuple[str, int]]:
    """``intro:4,verse:16,hook:8`` → ``[("intro", 4), ("verse", 16), ("hook", 8)]``."""
    out: list[tuple[str, int]] = []
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            raise ValueError(f"structure item {part!r} needs the form name:bars")
        name, bars = part.rsplit(":", 1)
        name = name.strip().lower()
        try:
            n = int(bars)
        except ValueError:
            raise ValueError(f"structure item {part!r}: bars must be a whole number") from None
        if n < 1:
            raise ValueError(f"structure item {part!r}: bars must be at least 1")
        out.append((name, n))
    if not out:
        raise ValueError("structure is empty")
    return out


def bars_ms(bars: int, bpm: float) -> int:
    return int(round(bars * BEATS_PER_BAR * 60000.0 / bpm))


def total_seconds(structure: str, bpm: float) -> float:
    return sum(bars_ms(n, bpm) for _, n in parse_structure(structure)) / 1000.0


def _split_long(name: str, ms: int) -> list[tuple[str, int]]:
    """A section over the provider's cap becomes equal parts under it — same total."""
    if ms <= SECTION_MAX_MS:
        return [(name, ms)]
    parts = -(-ms // SECTION_MAX_MS)
    base, extra = divmod(ms, parts)
    return [(f"{name} {i + 1}", base + (1 if i < extra else 0)) for i in range(parts)]


def build_plan(
    style: BeatStyle,
    *,
    bpm: float | None = None,
    key: str | None = None,
    structure: str | None = None,
    extra_positive: list[str] | None = None,
    extra_negative: list[str] | None = None,
) -> dict[str, Any]:
    """The ``composition_plan`` body for one render."""
    tempo = float(bpm or style.bpm)
    if tempo <= 0:
        raise ValueError("bpm must be positive")
    kname = key_name(key or style.key)
    layout = parse_structure(structure or style.structure)

    positive = list(style.positive) + list(extra_positive or [])
    positive += [f"{tempo:g} bpm", kname, "instrumental"]
    negative = list(style.negative) + list(extra_negative or [])
    for word in INSTRUMENTAL_NEGATIVE:
        if word not in negative:
            negative.append(word)

    sections: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for name, bars in layout:
        counts[name] = counts.get(name, 0) + 1
        ms = bars_ms(bars, tempo)
        if ms < SECTION_MIN_MS:
            ms = SECTION_MIN_MS
        local = list(style.sections.get(name, []))
        for part_name, part_ms in _split_long(name, ms):
            label = part_name if counts[name] == 1 else f"{part_name} ({counts[name]})"
            sections.append({
                "section_name": f"{label} — {bars} bars"[:100],
                "positive_local_styles": local,
                "negative_local_styles": [],
                "duration_ms": int(part_ms),
                "lines": [],
            })
    return {
        "positive_global_styles": positive,
        "negative_global_styles": negative,
        "sections": sections,
    }


def plan_seconds(plan: dict[str, Any]) -> float:
    return sum(int(s.get("duration_ms", 0)) for s in plan.get("sections", [])) / 1000.0


def prompt_text(
    style: BeatStyle,
    *,
    bpm: float | None = None,
    key: str | None = None,
    structure: str | None = None,
    extra_positive: list[str] | None = None,
    extra_negative: list[str] | None = None,
) -> str:
    """The same brief as free text — for ``--prompt-mode`` and the provider's plan preview."""
    tempo = float(bpm or style.bpm)
    layout = parse_structure(structure or style.structure)
    positive = list(style.positive) + list(extra_positive or [])
    negative = list(style.negative) + list(extra_negative or [])
    arrangement = " - ".join(f"{name} {bars} bars" for name, bars in layout)
    text = (
        f"{', '.join(positive)}, {tempo:g} bpm, {key_name(key or style.key)}, "
        f"instrumental only, no vocals, structure: {arrangement}"
    )
    for name in dict.fromkeys(n for n, _ in layout):
        local = style.sections.get(name)
        if local:
            text += f"; {name}: {', '.join(local)}"
    if negative:
        text += f"; avoid: {', '.join(negative)}"
    return text


def slug(style: BeatStyle, *, bpm: float | None = None, key: str | None = None, index: int = 1) -> str:
    tempo = float(bpm or style.bpm)
    k = key_short(key or style.key).lower().replace("#", "s")
    return f"{style.id}-{tempo:g}bpm-{k}-{index:02d}"


def next_index(out_dir: Path, style: BeatStyle, *, bpm: float | None = None, key: str | None = None) -> int:
    """One past the highest take already in ``out_dir`` for this style / tempo / key."""
    prefix = slug(style, bpm=bpm, key=key, index=1)[:-2]  # strip the "01"
    highest = 0
    if out_dir.exists():
        for f in out_dir.glob(f"{prefix}*.mp3"):
            m = re.match(re.escape(prefix) + r"(\d{2,})", f.name)
            if m:
                highest = max(highest, int(m.group(1)))
    return highest + 1
