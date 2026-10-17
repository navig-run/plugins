"""The blackbox's readers silently under-reported what it had recorded.

Three defects, all in the direction that makes a flight recorder useless
exactly when it is needed — it reports less than it holds, and the shortfall
is indistinguishable from "nothing was recorded":

1. `read_events` scanned only the last `limit * 4` lines, so every FILTERED
   query was silently truncated. Measured against a realistic stream (one
   COMMAND followed by a burst of its OUTPUT), the crash handler's
   `read_events(limit=10, event_type=COMMAND)` returned 4 — and returns 0 as
   soon as the ratio worsens. `recent_commands` is the first field anyone
   reads in a crash report.
2. Rotation blinded every reader: nothing ever opened `events.jsonl.1`, so a
   crash just after a rotation saw a near-empty history. Measured 18 events on
   disk, `event_count()` reporting 4.
3. `get_recorder(X)` ignored X whenever a singleton already existed, so
   `create_bundle(blackbox_dir=X)` read its events from one install and its
   crash reports from another.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import navig_blackbox.recorder as recorder_mod
from navig_blackbox.recorder import BlackboxRecorder, get_recorder
from navig_blackbox.types import EventType


@pytest.fixture(autouse=True)
def _isolate_singleton():
    """Never let one test's process recorder leak into the next."""
    original = recorder_mod._recorder
    recorder_mod._recorder = None
    yield
    recorder_mod._recorder = original


def _interleaved(rec: BlackboxRecorder, commands: int = 30, noise: int = 9) -> None:
    """One COMMAND, then a burst of its OUTPUT — what a real stream looks like."""
    for i in range(commands):
        rec.record(EventType.COMMAND, {"command": f"navig cmd-{i}"})
        for j in range(noise):
            rec.record(EventType.OUTPUT, {"message": f"out {i}.{j}"})


# ── 1. a filtered read must not stop at an arbitrary window ──────────────────


def test_filtered_read_returns_the_full_limit(tmp_path):
    rec = BlackboxRecorder(tmp_path)
    _interleaved(rec)

    got = rec.read_events(limit=10, event_type=EventType.COMMAND)

    assert len(got) == 10, "a filtered query was truncated to a window of recent lines"
    assert [e.payload["command"] for e in got] == [f"navig cmd-{i}" for i in range(29, 19, -1)]


def test_filtered_read_finds_matches_far_from_the_end(tmp_path):
    """The pathological shape: the only matches are old."""
    rec = BlackboxRecorder(tmp_path)
    rec.record(EventType.COMMAND, {"command": "the only command"})
    for i in range(400):
        rec.record(EventType.OUTPUT, {"message": f"noise {i}"})

    got = rec.read_events(limit=10, event_type=EventType.COMMAND)

    assert len(got) == 1, "an old match was invisible — reported as 'no commands ever'"
    assert got[0].payload["command"] == "the only command"


def test_crash_report_captures_recent_commands(tmp_path, monkeypatch):
    """End-to-end through the consumer this bug actually damaged."""
    from navig_blackbox.crash import record_crash

    rec = get_recorder(tmp_path)
    _interleaved(rec)

    report = record_crash(exc=ValueError("boom"), blackbox_dir=tmp_path)

    assert len(report.recent_commands) == 10, (
        f"crash report captured {len(report.recent_commands)} recent commands, not 10"
    )
    assert report.recent_commands[0] == "navig cmd-29"


# ── 2. rotation must not blind the readers ───────────────────────────────────


@pytest.fixture
def rotating(monkeypatch):
    """Force rotation at a tiny size so the boundary is reachable in a test."""
    monkeypatch.setattr(recorder_mod, "_MAX_SIZE_BYTES", 2000)


def _on_disk_lines(d) -> int:
    return sum(len(p.read_text(encoding="utf-8").splitlines()) for p in d.iterdir() if p.is_file())


def test_rotated_events_are_still_readable(tmp_path, rotating):
    rec = BlackboxRecorder(tmp_path)
    for i in range(60):
        rec.record(EventType.COMMAND, {"command": f"cmd-{i}"})

    assert rec._rotated_path.exists(), "the test did not actually trigger a rotation"

    on_disk = _on_disk_lines(tmp_path)
    assert rec.event_count() == on_disk, "event_count ignored the rotated generation"
    assert len(rec.tail(on_disk)) == on_disk, "tail() could not see past the rotation boundary"


def test_reads_stay_newest_first_across_the_rotation_boundary(tmp_path, rotating):
    rec = BlackboxRecorder(tmp_path)
    for i in range(60):
        rec.record(EventType.COMMAND, {"command": f"cmd-{i}"})

    got = rec.read_events(limit=_on_disk_lines(tmp_path))
    numbers = [int(e.payload["command"].split("-")[1]) for e in got]

    assert numbers == sorted(numbers, reverse=True), (
        "events came back out of order at the rotation boundary"
    )


def test_file_size_counts_every_event_file(tmp_path, rotating):
    rec = BlackboxRecorder(tmp_path)
    for i in range(60):
        rec.record(EventType.COMMAND, {"command": f"cmd-{i}"})

    on_disk = sum(p.stat().st_size for p in tmp_path.iterdir() if p.is_file())
    assert rec.file_size_mb() == pytest.approx(on_disk / (1024 * 1024))


def test_clear_removes_the_rotated_generation(tmp_path, rotating):
    """Data the user asked to delete must not survive where readers can see it."""
    rec = BlackboxRecorder(tmp_path)
    for i in range(60):
        rec.record(EventType.COMMAND, {"command": f"cmd-{i}"})
    assert rec._rotated_path.exists()

    rec.clear()

    assert rec.event_count() == 0
    assert rec.tail(100) == []
    assert not rec._rotated_path.exists(), "rotated events survived a clear()"


def test_rotation_and_readers_agree_on_the_filename(tmp_path, rotating):
    """One definition: a reader looking at a name the rotation never writes
    is the same bug wearing a different hat."""
    rec = BlackboxRecorder(tmp_path)
    for i in range(60):
        rec.record(EventType.COMMAND, {"command": f"cmd-{i}"})

    rotated = [p for p in tmp_path.iterdir() if p.name != "events.jsonl"]
    assert rotated == [rec._rotated_path]
    assert rec._rotated_path in rec._event_files()


# ── 3. an explicit directory must not be silently ignored ────────────────────


def test_explicit_dir_is_honoured_when_a_singleton_exists(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()

    singleton = get_recorder(first)
    other = get_recorder(second)

    assert other.blackbox_dir == second, "an explicit directory was dropped for the singleton"
    assert get_recorder() is singleton, "the process recorder was replaced as a side effect"


def test_bundle_reads_events_from_the_directory_it_was_given(tmp_path):
    """The bug in production terms: one bundle describing two installs."""
    from navig_blackbox.bundle import create_bundle

    live, target = tmp_path / "live", tmp_path / "target"
    live.mkdir()
    target.mkdir()

    get_recorder(live).record(EventType.COMMAND, {"command": "from the live dir"})
    BlackboxRecorder(target).record(EventType.COMMAND, {"command": "from the target dir"})

    bundle = create_bundle(since_hours=24, blackbox_dir=target)

    assert bundle.event_count() == 1
    assert bundle.events[0].payload["command"] == "from the target dir"


def test_same_dir_still_returns_the_cached_singleton(tmp_path):
    """Anti-vacuity: don't hand out a fresh recorder on every call."""
    first = get_recorder(tmp_path)
    assert get_recorder(tmp_path) is first
    assert get_recorder() is first


# ── anti-vacuity: the reads must still be bounded and still filter ───────────


def test_limit_is_still_respected(tmp_path):
    rec = BlackboxRecorder(tmp_path)
    for i in range(50):
        rec.record(EventType.COMMAND, {"command": f"cmd-{i}"})

    assert len(rec.read_events(limit=5)) == 5
    assert len(rec.tail(3)) == 3


def test_time_filters_still_exclude(tmp_path):
    rec = BlackboxRecorder(tmp_path)
    for i in range(10):
        rec.record(EventType.COMMAND, {"command": f"cmd-{i}"})

    future = datetime.now(timezone.utc) + timedelta(hours=1)
    past = datetime.now(timezone.utc) - timedelta(hours=1)

    assert rec.read_events(since=future) == []
    assert rec.read_events(until=past) == []
    assert len(rec.read_events(since=past, until=future)) == 10


def test_event_type_filter_still_excludes(tmp_path):
    rec = BlackboxRecorder(tmp_path)
    rec.record(EventType.COMMAND, {"command": "c"})
    rec.record(EventType.ERROR, {"message": "e"})

    only = rec.read_events(event_type=EventType.ERROR)
    assert len(only) == 1
    assert only[0].event_type == EventType.ERROR


def test_empty_and_malformed_streams_do_not_raise(tmp_path):
    rec = BlackboxRecorder(tmp_path)
    assert rec.read_events() == []
    assert rec.event_count() == 0
    assert rec.file_size_mb() == 0.0

    rec.record(EventType.COMMAND, {"command": "good"})
    with open(rec._events_path, "a", encoding="utf-8") as fh:
        fh.write("{not json at all\n\n")

    got = rec.read_events()
    assert len(got) == 1, "a corrupt line must be skipped, not lose the whole stream"
    assert got[0].payload["command"] == "good"
