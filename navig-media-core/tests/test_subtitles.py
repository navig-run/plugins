"""One SRT/ASS time formatter — parity with the copies it replaced, minus their carry bug."""

from __future__ import annotations

import pytest

from navig_media_core.media.subtitles import ass_timestamp, srt_timestamp


def _old_pipeline_srt(t: float) -> str:  # navig_pipeline.captions.to_srt.stamp, before
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int(round((s % 1) * 1000)):03d}"


def _old_fx_ass(seconds: float) -> str:  # navig_media_core.media.fx._ass_time, before
    seconds = max(0.0, seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{int(hours)}:{int(minutes):02d}:{secs:05.2f}"


GRID = [i * 0.037 for i in range(0, 4000)] + [3599.25, 3600.0, 3725.5, 7322.125]


def test_srt_matches_the_old_formatter_wherever_the_old_one_was_valid() -> None:
    diffs = [(t, _old_pipeline_srt(t), srt_timestamp(t)) for t in GRID
             if _old_pipeline_srt(t) != srt_timestamp(t)]
    # Any difference must be the old carry bug (a 4-digit millisecond field), nothing else.
    assert all(old.endswith("1000") for _t, old, _new in diffs), diffs[:5]


@pytest.mark.parametrize(("t", "want"), [
    (1.9996, "00:00:02,000"),      # old: 00:00:01,1000
    (59.9999, "00:01:00,000"),     # old: 00:00:59,1000
    (3599.9996, "01:00:00,000"),
    (0.0, "00:00:00,000"),
    (-3.0, "00:00:00,000"),
    (3725.5, "01:02:05,500"),
])
def test_srt_carries_into_the_next_second(t: float, want: str) -> None:
    assert srt_timestamp(t) == want


def test_ass_matches_the_old_formatter_wherever_the_old_one_was_valid() -> None:
    diffs = [(t, _old_fx_ass(t), ass_timestamp(t)) for t in GRID if _old_fx_ass(t) != ass_timestamp(t)]
    # Differences: the old ":60.00" carry, or binary-float half-way rounding of `05.2f`
    # (one centisecond) — never anything else.
    for _t, old, new in diffs:
        assert ":60." in old or abs(_cs(old) - _cs(new)) == 1, (old, new)


@pytest.mark.parametrize(("t", "want"), [
    (59.996, "0:01:00.00"),  # old: 0:00:60.00
    (3.456, "0:00:03.46"),
    (3725.5, "1:02:05.50"),
])
def test_ass_carries_into_the_next_minute(t: float, want: str) -> None:
    assert ass_timestamp(t) == want


def _cs(stamp: str) -> int:
    h, m, s = stamp.split(":")
    return round((int(h) * 3600 + int(m) * 60 + float(s)) * 100)
