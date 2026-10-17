"""Does a lyric fit a beat? Syllables per line against the time one bar gives you.

A verse line is one bar (or two, on a fast beat). A bar lasts ``240 / bpm`` seconds, and a
rapper comfortably lands roughly 3.2–5.1 syllables a second — so a line's syllable count
says whether it will sit in its bar, spill into the next, or leave a hole. That is the
whole model here; it is deliberately simple enough to reason about and to fix a text by.

Two lyric shapes are read:

* the beat-draft format — ``## RU`` / ``## EN`` / ``## FR`` language headings, ``### [VERSE 1 …]``
  section headings, one lyric line per bar;
* a plain lyric file — a title, an optional ``Language:`` line, then lines. It is read as
  one section in the language it declares or is written in.

Syllables are counted by vowel: Cyrillic vowels for Russian/Ukrainian, vowel groups for
English and French (with the silent final *e*). It is an estimate — good to a syllable or
two, which is the precision a bar has anyway.

Pure standard library: it needs no audio stack, so it lives with the text tools
(``navig text lyrics fit``); ``navig audio beat fit`` is an alias onto it.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Syllables per second a line can carry and still be rapped clearly. The upper bound is
# lifted for a fast "technique" delivery (FR at 140+), where a denser line is the point.
RATE_LO = 3.2
RATE_HI = 5.1
RATE_HI_FAST = 6.0
# The rate a text is written at when it sits in the middle of that band — used to say
# which tempo a text naturally wants.
RATE_MID = 4.4

RU_VOWELS = set("аеёиоуыэюяіїєАЕЁИОУЫЭЮЯІЇЄ")
LAT_VOWELS = "aeiouyàâäéèêëîïôöùûüÿœæ"
VERSE_RE = re.compile(r"КУПЛЕТ|VERSE|COUPLET", re.I)
HOOK_RE = re.compile(r"ХУК|ПРИПЕВ|HOOK|REFRAIN|CHORUS", re.I)
LANG_HEAD_RE = re.compile(r"^##\s+(RU|EN|FR|UK)\b")
FR_WORDS = {"je", "tu", "le", "la", "les", "des", "est", "et", "pas", "que", "qui", "c'est", "mon", "ma", "moi", "une", "dans", "pour", "avec", "mais"}
EN_WORDS = {"the", "and", "you", "i", "is", "it", "my", "me", "to", "of", "in", "that", "your", "on", "we", "with", "don't", "i'm"}


def detect_language(text: str) -> str:
    """``ru`` / ``fr`` / ``en`` from the letters and a handful of function words."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "en"
    cyr = sum(1 for c in letters if "а" <= c.lower() <= "я" or c.lower() in "ёіїє")
    if cyr / len(letters) > 0.3:
        return "ru"
    words = re.findall(r"[a-zà-ÿ']+", text.lower())
    fr = sum(w in FR_WORDS for w in words) + sum(1 for c in text if c in "éèêàçùâîôû")
    en = sum(w in EN_WORDS for w in words)
    return "fr" if fr > en else "en"


def _latin_word(word: str, lang: str) -> int:
    w = word.lower()
    groups = len(re.findall(f"[{LAT_VOWELS}]+", w))
    if groups > 1:
        if lang == "en" and w.endswith("e") and not w.endswith(("le", "ee")):
            groups -= 1
        if lang == "fr" and w.endswith(("e", "es")) and not w.endswith(("é", "és")):
            groups -= 1
    return max(groups, 1 if re.search(r"[a-zà-ÿ]", w) else 0)


def count_syllables(line: str, lang: str = "ru") -> int:
    """Syllables in one lyric line — stage directions in (…) and the ``/`` bar mark ignored."""
    line = re.sub(r"\(.*?\)", " ", line).replace("/", " ")
    total = 0
    for word in re.findall(r"[\w'’-]+", line):
        if any(c in RU_VOWELS for c in word):
            total += sum(c in RU_VOWELS for c in word)
        elif word.upper() == "VHS":
            total += 3
        elif word.isdigit():
            total += 2
        else:
            total += _latin_word(word, lang)
    return total


def syllable_range(bpm: float, *, bars_per_line: int = 1, fast: bool = False) -> tuple[int, int]:
    """The syllables one line can carry at this tempo."""
    seconds = 240.0 / bpm * bars_per_line
    return round(RATE_LO * seconds), round((RATE_HI_FAST if fast else RATE_HI) * seconds)


def natural_bpm(median_syllables: float, *, bars_per_line: int = 1) -> float:
    """The tempo at which a line of this length sits in the middle of its bar."""
    if median_syllables <= 0:
        return 0.0
    return 240.0 * bars_per_line * RATE_MID / median_syllables


@dataclass
class Line:
    text: str
    syllables: int


@dataclass
class Section:
    lang: str
    name: str
    kind: str  # verse | hook | other
    lines: list[Line] = field(default_factory=list)

    @property
    def counts(self) -> list[int]:
        return [ln.syllables for ln in self.lines]


def _is_lyric_line(s: str) -> bool:
    return bool(s) and s[0] not in "#|->*_`[" and not s.lower().startswith(("language:", "status:", "label:", "created:", "updated:"))


def parse(text: str) -> list[Section]:
    """Sections of a lyric document (beat-draft format, or a plain lyric as one section)."""
    sections: list[Section] = []
    lang: str | None = None
    current: Section | None = None
    structured = any(LANG_HEAD_RE.match(ln.strip()) for ln in text.splitlines())
    if not structured:
        body = [ln.strip() for ln in text.splitlines() if _is_lyric_line(ln.strip())]
        declared = re.search(r"^Language:\s*(\w+)", text, re.M)
        lang = {"russian": "ru", "french": "fr", "english": "en"}.get(
            (declared.group(1).lower() if declared else ""), None) or detect_language("\n".join(body))
        sec = Section(lang=lang, name="text", kind="verse")
        sec.lines = [Line(t, count_syllables(t, lang)) for t in body]
        return [sec] if sec.lines else []
    for raw in text.splitlines():
        s = raw.strip()
        m = LANG_HEAD_RE.match(s)
        if m:
            lang, current = m.group(1).lower(), None
            continue
        if s.startswith("## "):
            lang, current = None, None
            continue
        if s.startswith("### "):
            if lang is None:
                continue
            name = s.lstrip("#").strip().strip("[]")
            kind = "verse" if VERSE_RE.search(s) else ("hook" if HOOK_RE.search(s) else "other")
            current = Section(lang=lang, name=name, kind=kind)
            sections.append(current)
            continue
        if current is not None and _is_lyric_line(s):
            current.lines.append(Line(s, count_syllables(s, current.lang)))
    return [s for s in sections if s.lines]


@dataclass
class SectionFit:
    section: Section
    lo: int
    hi: int

    @property
    def out(self) -> list[Line]:
        # One syllable of slack either side — the counter is an estimate.
        return [ln for ln in self.section.lines if not self.lo - 1 <= ln.syllables <= self.hi + 1]

    def edits(self, expected_lines: int | None = None) -> list[str]:
        """Concrete changes that would make this section sit on the beat."""
        todo: list[str] = []
        for ln in self.out:
            if ln.syllables > self.hi + 1:
                todo.append(f"split or trim ({ln.syllables} > {self.hi}): {ln.text}")
            else:
                todo.append(f"fill, merge with the next line, or keep as a deliberate punch ({ln.syllables} < {self.lo}): {ln.text}")
        n = len(self.section.lines)
        if expected_lines and n != expected_lines:
            verb = "add" if n < expected_lines else "cut"
            todo.append(f"{verb} {abs(expected_lines - n)} line(s): {n} lines for a {expected_lines}-bar section")
        return todo


@dataclass
class FitReport:
    path: str
    bpm: float | None
    bars_per_line: int
    sections: list[SectionFit]

    @property
    def verse_counts(self) -> list[int]:
        return [c for sf in self.sections if sf.section.kind == "verse" for c in sf.section.counts]

    @property
    def median(self) -> float:
        counts = self.verse_counts or [c for sf in self.sections for c in sf.section.counts]
        return float(statistics.median(counts)) if counts else 0.0

    @property
    def natural_bpm(self) -> float:
        return natural_bpm(self.median, bars_per_line=self.bars_per_line)

    @property
    def out_count(self) -> int:
        return sum(len(sf.out) for sf in self.sections if sf.section.kind == "verse")

    @property
    def languages(self) -> list[str]:
        return sorted({sf.section.lang for sf in self.sections})

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path, "bpm": self.bpm, "bars_per_line": self.bars_per_line,
            "languages": self.languages, "median_syllables": self.median,
            "natural_bpm": round(self.natural_bpm, 1), "verse_lines_out": self.out_count,
            "sections": [{
                "lang": sf.section.lang, "name": sf.section.name, "kind": sf.section.kind,
                "lines": len(sf.section.lines), "range": [sf.lo, sf.hi],
                "min": min(sf.section.counts), "median": statistics.median(sf.section.counts),
                "max": max(sf.section.counts),
                "out": [{"syllables": ln.syllables, "text": ln.text} for ln in sf.out],
            } for sf in self.sections],
        }


def fit(path: Path | str, *, bpm: float | None = None, bars_per_line: int = 1,
        fast: bool = False) -> FitReport:
    """Measure every section of a lyric file against ``bpm`` (or its own natural tempo)."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    sections = parse(text)
    probe = FitReport(path=str(path), bpm=bpm, bars_per_line=bars_per_line,
                      sections=[SectionFit(s, 0, 0) for s in sections])
    tempo = bpm or (probe.natural_bpm or 90.0)
    lo, hi = syllable_range(tempo, bars_per_line=bars_per_line, fast=fast)
    return FitReport(path=str(path), bpm=bpm, bars_per_line=bars_per_line,
                     sections=[SectionFit(s, lo, hi) for s in sections])


def nearest_beat(natural: float, beats: list[tuple[str, float]]) -> tuple[str, float, str] | None:
    """The beat whose tempo is closest to a text's natural tempo, at ½×, 1× or 2×.

    A text of short lines (6–8 syllables) reads as 150+ bpm at one line per bar — which on a
    boom-bap beat means two lines per bar. Comparing at half and double time is what turns
    "176 bpm" into "the 88 bpm beat, two lines to a bar".
    """
    import math

    if natural <= 0 or not beats:
        return None
    best: tuple[float, str, float, str] | None = None
    # A straight match wins a near-tie: "76 bpm, one line per bar" is a better answer for a
    # 75 bpm text than "150 bpm counted at half", even though both are arithmetically right.
    options = ((1.0, "one line per bar", 0.0), (0.5, "two lines per bar", 0.04),
               (2.0, "one line per bar, counted at half-time", 0.04))
    for name, bpm in beats:
        for factor, how, penalty in options:
            d = abs(math.log2(natural * factor / bpm)) + penalty
            if best is None or d < best[0]:
                best = (d, name, bpm, how)
    return (best[1], best[2], best[3]) if best else None


def library_beats(dirs: list[Path]) -> list[tuple[str, float]]:
    """(name, target bpm) for every rendered beat under ``dirs`` (from its .json sidecar)."""
    import json

    out: list[tuple[str, float]] = []
    for d in dirs:
        for side in sorted(Path(d).rglob("*.json")):
            try:
                rec = json.loads(side.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            bpm = (rec.get("target") or {}).get("bpm") if isinstance(rec, dict) else None
            if bpm:
                out.append((side.stem, float(bpm)))
    return out


def tempo_band(bpm: float) -> str:
    """How a natural tempo reads to a producer."""
    if bpm <= 0:
        return "—"
    if bpm < 80:
        return "slow boom bap / halftime (70–80)"
    if bpm < 98:
        return "boom bap (80–98)"
    if bpm < 125:
        return "mid-tempo (98–125) — or halftime of a 200+ beat"
    return "fast (125+) — or a halftime beat counted at half"
