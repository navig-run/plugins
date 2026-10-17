"""Dropped frames → the Grid Pigeon's lag line.

OBS counts three kinds of lost frames, all cumulative since it started (or
since the output started):

* ``GetStats.renderSkippedFrames`` — the compositor missed a frame (GPU);
* ``GetStats.outputSkippedFrames`` — the encoder could not keep up (CPU/NVENC);
* ``GetStreamStatus.outputSkippedFrames`` — frames dropped on the way out
  (the network), only while streaming.

:class:`FrameWatch` turns those totals into a decision: between two samples,
did enough frames go missing to be *seen*? A handful is noise; a burst is a
hitch the audience noticed, and the pigeon gets to comment on it. A counter
that goes BACKWARDS (OBS restarted, a new stream began) is a new baseline,
never a burst.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# A burst the audience sees: at least this many frames lost between two
# samples (half a second at 60 fps), and at least this share of the frames
# that should have been drawn in that time.
MIN_FRAMES = 30
MIN_SHARE = 0.05
COOLDOWN_S = 180.0  # one complaint per three minutes, however bad it gets
POLL_SECONDS = 5.0

KINDS = ("render", "encode", "network")


@dataclass
class FrameWatch:
    min_frames: int = MIN_FRAMES
    min_share: float = MIN_SHARE
    cooldown_s: float = COOLDOWN_S
    _last: dict[str, tuple[int, int]] = field(default_factory=dict)
    _fired_at: float | None = None
    # What the last sample lost, per kind: (frames lost, frames that should
    # have been drawn) — what the stream health director reads.
    deltas: dict[str, tuple[int, int]] = field(default_factory=dict)

    def update(self, sample: dict[str, tuple[int, int]], now: float) -> str | None:
        """Feed one sample ``{kind: (skipped_total, total_frames)}``.

        Returns the kind that burst (``render`` / ``encode`` / ``network``)
        when the pigeon should speak, else None.
        """
        burst: str | None = None
        worst = 0
        self.deltas = {}
        for kind, (skipped, total) in sample.items():
            prev = self._last.get(kind)
            self._last[kind] = (skipped, total)
            if prev is None:
                continue  # the first sample is only a baseline
            d_skip, d_total = skipped - prev[0], total - prev[1]
            if d_skip < 0 or d_total < 0:
                continue  # a counter reset: new baseline, not a burst
            self.deltas[kind] = (d_skip, d_total)
            share = d_skip / max(d_total, d_skip, 1)
            if d_skip >= self.min_frames and share >= self.min_share and d_skip > worst:
                burst, worst = kind, d_skip
        if burst is None:
            return None
        if self._fired_at is not None and now - self._fired_at < self.cooldown_s:
            return None
        self._fired_at = now
        return burst


def sample_from(stats: dict[str, Any], stream: dict[str, Any] | None) -> dict[str, tuple[int, int]]:
    """``GetStats`` (+ ``GetStreamStatus`` while live) → a :meth:`FrameWatch.update` sample."""

    def n(d: dict[str, Any], key: str) -> int:
        try:
            return int(d.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    out = {
        "render": (n(stats, "renderSkippedFrames"), n(stats, "renderTotalFrames")),
        "encode": (n(stats, "outputSkippedFrames"), n(stats, "outputTotalFrames")),
    }
    if stream and stream.get("outputActive"):
        out["network"] = (n(stream, "outputSkippedFrames"), n(stream, "outputTotalFrames"))
    return out


# ── the stream health director (opt-in: obs.show.health_director) ──────────
#
# When the STREAM itself is failing — frames dropped on the way out (network)
# or not encoded in time (encode), for two looks running — the audience is
# watching a stutter. The director cuts to the BRB scene, and comes back once
# three looks running are clean, if nobody moved the stage meanwhile. GPU
# (render) drops are the pigeon's lag joke, never a reason to leave the scene.

DIRECTOR_KINDS = ("network", "encode")
DIRECTOR_MIN_FRAMES = 30
DIRECTOR_MIN_SHARE = 0.10
DIRECTOR_BAD_TO_CUT = 2  # ~10 s at a 5 s look
DIRECTOR_GOOD_TO_RESTORE = 3  # ~15 s
DIRECTOR_COOLDOWN_S = 60.0
NEVER_LEAVE = ("14 Emergency Cut", "12 Offline")


@dataclass
class Director:
    brb_scene: str = "10 Signal Lost"
    never_leave: tuple[str, ...] = NEVER_LEAVE
    _bad: int = 0
    _good: int = 0
    _cut_from: str | None = None
    _restored_at: float | None = None

    @property
    def cut(self) -> bool:
        return self._cut_from is not None

    def observe(
        self, deltas: dict[str, tuple[int, int]], *, streaming: bool, scene: str, now: float,
    ) -> tuple[str, str] | None:
        """One look. Returns ("cut", brb) / ("restore", scene) to act on, or None."""
        if not streaming:
            self._bad = self._good = 0
            self._cut_from = None  # the show ended on BRB: nothing to restore
            return None
        bad = any(
            d >= DIRECTOR_MIN_FRAMES and d / max(t, d, 1) >= DIRECTOR_MIN_SHARE
            for k, (d, t) in deltas.items()
            if k in DIRECTOR_KINDS
        )
        if self._cut_from is None:
            self._bad = self._bad + 1 if bad else 0
            if self._bad < DIRECTOR_BAD_TO_CUT:
                return None
            if scene == self.brb_scene or scene in self.never_leave or not scene:
                return None
            if self._restored_at is not None and now - self._restored_at < DIRECTOR_COOLDOWN_S:
                return None
            self._cut_from, self._bad, self._good = scene, 0, 0
            return ("cut", self.brb_scene)
        if scene != self.brb_scene:
            # The operator moved the stage while we were on BRB: their call.
            self._cut_from, self._good = None, 0
            return None
        self._good = 0 if bad else self._good + 1
        if self._good < DIRECTOR_GOOD_TO_RESTORE:
            return None
        back, self._cut_from, self._good = self._cut_from, None, 0
        self._restored_at = now
        return ("restore", back)
