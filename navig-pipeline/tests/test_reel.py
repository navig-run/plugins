"""Cutting picture to narration.

`plan_shots` is where a reel is either in sync or not, and it is pure, so it is tested
without ffmpeg or a browser. The property that matters is in
:func:`test_shots_always_add_up_to_the_narration`: whatever the author wrote, the shots
must total exactly the time the line took to speak. Anything else leaves the picture
cutting mid-sentence, which is the one artefact a viewer notices immediately.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from navig_pipeline.reel import (
    MIN_SHOT_S,
    ReelError,
    PlannedShot,
    plan_shots,
    scenario_for,
)


@dataclass(frozen=True)
class FakeShot:
    """Stands in for navig_audio.podcast.scenario.Shot — same duck type, no dependency."""

    url: str | None = "http://h/"
    image: str | None = None
    secs: float | None = None
    motion: str = "none"


# ── the invariant ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("written", [[1.0, 1.0], [3.4, 4.0, 3.0], [10.0], [0.1, 9.9]])
@pytest.mark.parametrize("spoken", [2.5, 7.0, 31.25])
def test_shots_always_add_up_to_the_narration(written: list[float], spoken: float) -> None:
    shots = [FakeShot(secs=s) for s in written]
    planned, _ = plan_shots(shots, spoken)
    assert sum(p.seconds for p in planned) == pytest.approx(spoken, abs=1e-6)


def test_relative_pacing_is_preserved_when_scaling() -> None:
    # Written 1:2:1 over 8s of speech must come out 2:4:2, not 3.4:4:3.
    planned, _ = plan_shots([FakeShot(secs=1), FakeShot(secs=2), FakeShot(secs=1)], 8.0)
    assert [round(p.seconds, 3) for p in planned] == [2.0, 4.0, 2.0]


def test_shots_with_no_secs_split_the_track_evenly() -> None:
    planned, _ = plan_shots([FakeShot(), FakeShot(), FakeShot()], 9.0)
    assert [round(p.seconds, 3) for p in planned] == [3.0, 3.0, 3.0]


def test_a_mix_of_written_and_unwritten_still_fits() -> None:
    planned, _ = plan_shots([FakeShot(secs=4), FakeShot(), FakeShot(secs=2)], 10.0)
    assert sum(p.seconds for p in planned) == pytest.approx(10.0, abs=1e-6)
    # The written 4 is still twice the written 2.
    assert planned[0].seconds == pytest.approx(planned[2].seconds * 2, rel=1e-3)


def test_a_very_short_track_does_not_produce_unreadable_flashes() -> None:
    # Six shots over 0.9s would be 0.15s each; the floor lifts them, and the total is
    # renormalised so sync still holds.
    planned, _ = plan_shots([FakeShot() for _ in range(6)], 0.9)
    assert sum(p.seconds for p in planned) == pytest.approx(0.9, abs=1e-6)
    assert len(planned) == 6


def test_the_floor_applies_when_there_is_room_for_it() -> None:
    planned, _ = plan_shots([FakeShot(secs=100), FakeShot(secs=0.01)], 20.0)
    assert min(p.seconds for p in planned) >= MIN_SHOT_S - 1e-6


# ── telling the author when their pacing was overridden ───────────────────────


def test_a_large_override_is_reported() -> None:
    # 4s written, 12s spoken — the author should hear about it rather than wonder.
    _, note = plan_shots([FakeShot(secs=2), FakeShot(secs=2)], 12.0, track="03")
    assert note and "03" in note and "stretched" in note


def test_a_faithful_pacing_is_not_nagged_about() -> None:
    _, note = plan_shots([FakeShot(secs=2), FakeShot(secs=2)], 4.1)
    assert note is None


def test_no_note_when_the_author_wrote_no_durations() -> None:
    # Nothing was overridden, so there is nothing to report.
    _, note = plan_shots([FakeShot(), FakeShot()], 30.0)
    assert note is None


# ── refusals ──────────────────────────────────────────────────────────────────


def test_a_track_with_no_shots_is_refused() -> None:
    with pytest.raises(ReelError, match="no \\[shot"):
        plan_shots([], 5.0, track="02")


def test_a_track_with_no_narration_is_refused() -> None:
    with pytest.raises(ReelError, match="no narration"):
        plan_shots([FakeShot()], 0.0)


# ── sibling scenario resolution ───────────────────────────────────────────────


def test_a_language_infix_is_swapped_not_appended() -> None:
    assert scenario_for(Path("ep000.fr.md"), "en") == Path("ep000.en.md")


def test_a_bare_name_gains_the_language() -> None:
    assert scenario_for(Path("grid.md"), "fr") == Path("grid.fr.md")


def test_a_dotted_name_that_is_not_a_language_is_left_alone() -> None:
    # "ep000.draft.md" — "draft" is not a language tag, so it must not be replaced.
    assert scenario_for(Path("ep000.draft.md"), "en") == Path("ep000.draft.en.md")


def test_the_directory_is_preserved() -> None:
    assert scenario_for(Path("a/b/grid.fr.md"), "en").parent == Path("a/b")


def test_a_planned_shot_serialises() -> None:
    assert PlannedShot(0, 1.23456, url="http://h/").to_dict()["seconds"] == 1.235
