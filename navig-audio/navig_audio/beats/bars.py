"""Where the beat actually is, bar by bar.

A generated beat does not always follow the plan it was rendered from: a model asked for a
4-bar intro can return 8, a planned hook can come back as a near-silent stretch, and a
"full" track can hold its drums back for forty seconds. Lyrics timed from the plan then
land in the wrong place. This measures the file instead: one loudness value per bar, the
bars grouped into blocks (``build``, ``full``, ``mid``, ``drop``, ``tail``), each with the
bar number and the timecode it starts at — a cue sheet taken from the audio itself.

The bar grid is phase-aligned to the detected downbeat, so bar 1 starts where the music's
first bar starts rather than at t=0.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# A bar's level relative to the loudest bar, in 2 dB steps: 9 = loudest, 0 = ≥18 dB down.
STEP_DB = 2.0
FULL_MIN = 7  # within ~4 dB of the loudest bar — the whole beat is playing
QUIET_MAX = 3  # ≥ ~12 dB down — the beat has dropped out


@dataclass
class Block:
    label: str  # build | full | mid | drop | tail
    start_bar: int  # 1-based, inclusive
    end_bar: int  # inclusive
    start_s: float
    end_s: float
    level: int  # median digit of the block

    @property
    def bars(self) -> int:
        return self.end_bar - self.start_bar + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label, "start_bar": self.start_bar, "end_bar": self.end_bar,
            "bars": self.bars, "start_s": round(self.start_s, 2), "end_s": round(self.end_s, 2),
            "level": self.level,
        }


@dataclass
class BarMap:
    bpm: float
    bar_s: float
    phase_s: float
    duration_s: float
    levels_db: list[float] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)

    @property
    def digits(self) -> str:
        if not self.levels_db:
            return ""
        top = max(self.levels_db)
        return "".join(str(_digit(top, v)) for v in self.levels_db)

    def bar_time(self, bar: int) -> float:
        """Start of a (1-based) bar in seconds."""
        return self.phase_s + (bar - 1) * self.bar_s

    def strip(self, per_row: int = 16) -> list[str]:
        """Rows of the digit strip: ``bar  17 @  52.5s  7989 8968 8989 8989``."""
        d = self.digits
        rows = []
        for i in range(0, len(d), per_row):
            chunk = d[i:i + per_row]
            groups = " ".join(chunk[j:j + 4] for j in range(0, len(chunk), 4))
            rows.append(f"bar {i + 1:>3} @ {_mmss(self.bar_time(i + 1)):>7}  {groups}")
        return rows

    def block(self, spec: str) -> Block:
        """``full`` / ``full:2`` / ``drop`` / ``tail`` → that block (1-based occurrence).

        ``build|full:1`` names fallbacks, first that exists wins: a render that starts
        with the full beat has no build, and an effect meant for the intro should then
        land where the beat starts rather than fail the whole sheet.
        """
        if "|" in spec:
            for alt in spec.split("|"):
                try:
                    return self.block(alt.strip())
                except ValueError:
                    continue
            raise ValueError(f"none of {spec!r} — blocks: {', '.join(b.label for b in self.blocks)}")
        label, _, nth = spec.partition(":")
        n = int(nth) if nth else 1
        hits = [b for b in self.blocks if b.label == label.strip()]
        if len(hits) < n:
            raise ValueError(f"no block {spec!r} — blocks: {', '.join(b.label for b in self.blocks)}")
        return hits[n - 1]

    def cue_table(self) -> str:
        lines = ["| Block | Bars | From | To | Level |", "|---|---|---|---|---|"]
        for b in self.blocks:
            lines.append(
                f"| {b.label} | {b.start_bar}–{b.end_bar} ({b.bars}) | {_mmss(b.start_s)} | "
                f"{_mmss(b.end_s)} | {b.level} |"
            )
        return "\n".join(lines)

    def to_markdown(self) -> str:
        return "\n".join([
            f"## Bar map — {self.bpm:g} bpm, bar = {self.bar_s:.3f} s, bar 1 at {self.phase_s:.2f} s",
            "",
            "Level per bar: 9 = loudest bar, each step 2 dB quieter.",
            "",
            "```",
            *self.strip(),
            "```",
            "",
            self.cue_table(),
            "",
        ])

    def to_dict(self) -> dict[str, Any]:
        return {
            "bpm": self.bpm, "bar_s": round(self.bar_s, 4), "phase_s": round(self.phase_s, 3),
            "bars": len(self.levels_db), "strip": self.digits,
            "blocks": [b.to_dict() for b in self.blocks],
        }


def _digit(top: float, value: float) -> int:
    return max(0, min(9, 9 - int(round((top - value) / STEP_DB))))


def _mmss(seconds: float) -> str:
    m, s = divmod(max(0.0, seconds), 60)
    return f"{int(m)}:{s:04.1f}"


def classify(digits: list[int]) -> list[tuple[str, int, int]]:
    """Runs of bars → ``(label, start_index, end_index)``, 0-based inclusive.

    ``full`` bars are the beat playing; anything before the first full run is the
    ``build``, anything after the last is the ``tail``; between them a quiet run is a
    ``drop`` and an in-between level is ``mid``.
    """
    def kind(d: int) -> str:
        return "full" if d >= FULL_MIN else ("quiet" if d <= QUIET_MAX else "mid")

    runs: list[list[Any]] = []
    for i, d in enumerate(digits):
        k = kind(d)
        if runs and runs[-1][0] == k:
            runs[-1][2] = i
        else:
            runs.append([k, i, i])
    # A one-bar loud blip (a crash, a fill into the next section) is not a section: fold it
    # into the run before it. A one-bar QUIET run is kept — a bar of silence mid-hook is a
    # real cue, the one a clip gets cut on.
    folded: list[list[Any]] = []
    for r in runs:
        if folded and r[1] == r[2] and r[0] in ("full", "mid"):
            folded[-1][2] = r[2]
        elif folded and folded[-1][0] == r[0]:
            folded[-1][2] = r[2]
        else:
            folded.append(list(r))
    runs = folded
    fulls = [i for i, r in enumerate(runs) if r[0] == "full"]
    out: list[tuple[str, int, int]] = []
    for i, (k, a, b) in enumerate(runs):
        if k == "full":
            label = "full"
        elif not fulls or i < fulls[0]:
            label = "build"
        elif i > fulls[-1]:
            label = "tail"
        else:
            label = "drop" if k == "quiet" else "mid"
        if out and out[-1][0] == label and label in ("build", "tail"):
            out[-1] = (label, out[-1][1], b)
        else:
            out.append((label, a, b))
    return out


def bar_map(path: Path | str, bpm: float, *, sample_rate: int = 22050) -> BarMap:
    """Measure ``path`` bar by bar at ``bpm`` (4 beats per bar).

    For a halftime beat pass the *felt* tempo (70 for a 140 track) to get bars the way a
    rapper counts them.
    """
    import numpy as np

    from navig_generate.media.beats import decode, detect

    if bpm <= 0:
        raise ValueError("bpm must be positive")
    samples = decode(path, sample_rate=sample_rate)
    grid = detect(path, bpm=bpm)
    bar_s = 240.0 / bpm
    phase = (grid.downbeats[0] % bar_s) if grid.downbeats else 0.0
    duration = samples.size / sample_rate
    n = int((duration - phase) // bar_s)
    levels: list[float] = []
    for i in range(n):
        a = int((phase + i * bar_s) * sample_rate)
        b = int((phase + (i + 1) * bar_s) * sample_rate)
        seg = samples[a:b].astype("float64")
        levels.append(20.0 * float(np.log10(np.sqrt(np.mean(seg ** 2)) + 1e-9)))
    bm = BarMap(bpm=bpm, bar_s=bar_s, phase_s=phase, duration_s=duration, levels_db=levels)
    if levels:
        top = max(levels)
        digits = [_digit(top, v) for v in levels]
        for label, a, b in classify(digits):
            chunk = sorted(digits[a:b + 1])
            bm.blocks.append(Block(
                label=label, start_bar=a + 1, end_bar=b + 1,
                start_s=bm.bar_time(a + 1), end_s=bm.bar_time(b + 2),
                level=chunk[len(chunk) // 2],
            ))
    return bm
