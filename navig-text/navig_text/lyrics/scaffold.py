"""An empty lyric, cut to where the beat actually is.

A generated beat rarely follows the plan it was rendered from — the intro runs long, the
hook comes back as a near-silent stretch — so a text written to the plan lands in the wrong
place. The bar map ``navig audio beat analyse --bars`` measures (and ``beat gen`` stores in
every sidecar as ``measured.bars``) says where the blocks really are. This turns that map
into a skeleton: one section per block, one empty slot per line, each slot showing the
syllables it can carry.

The mapping is deliberately plain, so an author can predict it and override it:

* ``build`` → intro, ``tail`` → outro (spoken, optional);
* a ``drop`` or ``mid`` block → a hook — the stretch where the beat steps back;
* a ``full`` block → 16-line verses; what is left is a hook (≥ 4 lines) or a
  pre-hook pickup (1–3 lines); a full block of 10–15 lines is a short verse.

Slots start with ``_``, which ``fit`` never counts, so a half-written text still measures
only its real lines.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from navig_text.lyrics.fit import syllable_range

VERSE_LINES = 16
HOOK_MIN = 4
SHORT_VERSE_MIN = 10

LABELS: dict[str, dict[str, str]] = {
    "ru": {"intro": "ИНТРО", "verse": "КУПЛЕТ", "hook": "ХУК", "pre": "ПРЕ-ХУК", "outro": "АУТРО",
           "bars": "тактов", "bar": "такт", "spoken": "говорить, по желанию"},
    "en": {"intro": "INTRO", "verse": "VERSE", "hook": "HOOK", "pre": "PRE-HOOK", "outro": "OUTRO",
           "bars": "bars", "bar": "bar", "spoken": "spoken, optional"},
    "fr": {"intro": "INTRO", "verse": "COUPLET", "hook": "REFRAIN", "pre": "PRÉ-REFRAIN", "outro": "OUTRO",
           "bars": "mesures", "bar": "mesure", "spoken": "parlé, facultatif"},
}


class ScaffoldError(ValueError):
    """A bar map the scaffold cannot use."""


@dataclass
class Slot:
    kind: str  # intro | verse | hook | pre | outro
    number: int  # 1-based within its kind: verse 1, verse 2 …
    start_bar: int
    lines: int
    bars_per_line: int
    start_s: float
    block: str  # the measured block it sits on

    @property
    def end_bar(self) -> int:
        return self.start_bar + self.lines * self.bars_per_line - 1


def read_bar_map(path: Path) -> dict[str, Any] | None:
    """The measured bar map for ``path`` — a beat (its ``.json`` sidecar is read) or a JSON file.

    Accepts a sidecar (``{"measured": {"bars": …}}``), ``beat analyse --json`` output
    (``{"bars": …}``) or a bare bar map. ``None`` when there is nothing measured to read.
    """
    src = path if path.suffix.lower() == ".json" else path.with_suffix(".json")
    if not src.is_file():
        return None
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ScaffoldError(f"{src.name}: not readable JSON ({exc})") from exc
    if not isinstance(data, dict):
        return None
    for candidate in ((data.get("measured") or {}).get("bars"), data.get("bars"), data):
        if isinstance(candidate, dict) and candidate.get("blocks") and candidate.get("bar_s"):
            return candidate
    return None


def sidecar_bpm(path: Path) -> float | None:
    """The tempo a beat was rendered at, from its sidecar (``target.bpm``), if it has one."""
    src = path if path.suffix.lower() == ".json" else path.with_suffix(".json")
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    bpm = (data.get("target") or {}).get("bpm") if isinstance(data, dict) else None
    return float(bpm) if bpm else None


def plan(bars: dict[str, Any], *, bars_per_line: int = 1) -> list[Slot]:
    """Sections, in order, from a measured bar map."""
    try:
        bar_s = float(bars["bar_s"])
        phase = float(bars.get("phase_s", 0.0))
        blocks = list(bars["blocks"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ScaffoldError(f"not a bar map: {exc}") from exc
    if bars_per_line < 1:
        raise ScaffoldError("bars_per_line must be at least 1")

    slots: list[Slot] = []
    counts: dict[str, int] = {}

    def add(kind: str, start_bar: int, lines: int, block: str) -> None:
        counts[kind] = counts.get(kind, 0) + 1
        slots.append(Slot(kind=kind, number=counts[kind], start_bar=start_bar, lines=lines,
                          bars_per_line=bars_per_line, start_s=phase + (start_bar - 1) * bar_s,
                          block=block))

    for b in blocks:
        label = str(b.get("label", ""))
        start = int(b["start_bar"])
        n = int(b.get("bars") or (int(b["end_bar"]) - start + 1)) // bars_per_line
        if n <= 0:
            continue
        if label == "build":
            add("intro", start, n, label)
        elif label == "tail":
            add("outro", start, n, label)
        elif label in ("drop", "mid"):
            add("hook", start, n, label)
        else:
            bar = start
            while n >= VERSE_LINES:
                add("verse", bar, VERSE_LINES, label)
                bar += VERSE_LINES * bars_per_line
                n -= VERSE_LINES
            if n >= SHORT_VERSE_MIN:
                add("verse", bar, n, label)
            elif n >= HOOK_MIN:
                add("hook", bar, n, label)
            elif n:
                add("pre", bar, n, label)
    return slots


def _mmss(seconds: float) -> str:
    m, s = divmod(max(0.0, seconds), 60)
    return f"{int(m)}:{s:04.1f}"


def render(slots: list[Slot], bars: dict[str, Any], *, langs: list[str], title: str, beat: str,
           bpm: float, bars_per_line: int = 1, fast: bool = False) -> str:
    """The skeleton as Markdown in the beat-draft format ``fit`` reads."""
    unknown = [lang for lang in langs if lang not in LABELS]
    if unknown:
        raise ScaffoldError(f"no section labels for {', '.join(unknown)} — known: {', '.join(LABELS)}")
    lo, hi = syllable_range(bpm, bars_per_line=bars_per_line, fast=fast)
    bar_s = float(bars["bar_s"])
    per = "one bar" if bars_per_line == 1 else f"{bars_per_line} bars"
    out = [
        f"# {title}",
        "",
        f"**Beat:** `{beat}` — {bpm:g} bpm, bar = {bar_s:.2f} s, bar 1 at {float(bars.get('phase_s', 0)):.2f} s.",
        f"**One line = {per} → {lo}–{hi} syllables.** Every `_` line is an empty slot: `fit` does not",
        "count it. Replace each with one lyric line; an empty line between groups of four is a phrase.",
        "",
        "## Cue sheet — measured from the file",
        "",
        "| Section | Lines | Bars | From | Block |",
        "|---|---|---|---|---|",
    ]
    en = LABELS["en"]
    for s in slots:
        name = en[s.kind] + (f" {s.number}" if s.kind in ("verse", "hook") else "")
        out.append(f"| {name} | {s.lines} | {s.start_bar}–{s.end_bar} | {_mmss(s.start_s)} | {s.block} |")
    for lang in langs:
        lab = LABELS[lang]
        out += ["", "---", "", f"## {lang.upper()}", ""]
        for s in slots:
            name = lab[s.kind] + (f" {s.number}" if s.kind in ("verse", "hook") else "")
            span = s.lines * s.bars_per_line
            note = f" — {lab['spoken']}" if s.kind in ("intro", "outro") else ""
            out.append(f"### [{name} — {span} {lab['bars'] if span > 1 else lab['bar']} · "
                       f"{s.start_bar}–{s.end_bar} · {_mmss(s.start_s)}{note}]")
            out.append("")
            for i in range(s.lines):
                out.append(f"_ {i + 1} · {lo}–{hi}")
                if (i + 1) % 4 == 0 and i + 1 < s.lines:
                    out.append("")
            out.append("")
    return "\n".join(out).rstrip() + "\n"
