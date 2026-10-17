"""NAVIG Blackbox Recorder — append-only JSONL event stream.

Storage: ~/.navig/blackbox/events.jsonl
Format : One JSON object per line (JSONL).
Rotation: File rotated when it exceeds 50 MB (old renamed to events.jsonl.1).
         Two generations are kept, and readers span BOTH — a flight recorder
         that forgets at the rotation boundary is empty exactly when an
         incident happens to follow one.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .types import BlackboxEvent, EventType

__all__ = ["BlackboxRecorder", "get_recorder"]

_MAX_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB
_EVENTS_FILE = "events.jsonl"


class BlackboxRecorder:
    """Append-only event recorder using JSONL storage.

    Parameters
    ----------
    blackbox_dir : Path to the blackbox directory.
    """

    def __init__(self, blackbox_dir: Path) -> None:
        self.blackbox_dir = blackbox_dir
        self._events_path = blackbox_dir / _EVENTS_FILE
        # Defined ONCE — the rotation and every reader must agree on this name.
        self._rotated_path = self._events_path.with_suffix(".jsonl.1")
        self._enabled = True

    # ── Control ──────────────────────────────────────────────────────────────

    def enable(self) -> None:
        self._enabled = True

    def disable(self) -> None:
        self._enabled = False

    def is_enabled(self) -> bool:
        return self._enabled

    def is_sealed(self) -> bool:
        """True when a SEALED marker freezes this directory against appends."""
        from .seal import is_sealed as _is_sealed

        return _is_sealed(self.blackbox_dir)

    # ── Recording ─────────────────────────────────────────────────────────────

    def record(
        self,
        event_type: EventType,
        payload: dict,
        tags: list[str] | None = None,
        source: str = "navig",
    ) -> BlackboxEvent | None:
        """Append an event to the JSONL stream.

        Returns the event, or None if recording is disabled or the blackbox is
        SEALED.

        The seal is enforced here because that is what it has always claimed to
        be: ``seal.py`` documents "a sealed bundle cannot be appended to
        (recorders check ``is_sealed``)" and ``navig blackbox seal`` prints
        "recording is now frozen until `unseal`". Nothing checked it. Its only
        readers were a status line and a test asserting the marker file
        round-trips, so an investigator who sealed the state to preserve it
        watched the daemon keep appending to the evidence — with
        ``navig blackbox status`` cheerfully reporting ``sealed yes``.
        """
        if not self._enabled:
            return None
        if self.is_sealed():
            return None

        event = BlackboxEvent.create(event_type, payload, tags, source)
        self._write(event)
        return event

    def _write(self, event: BlackboxEvent) -> None:
        self.blackbox_dir.mkdir(parents=True, exist_ok=True)
        self._maybe_rotate()
        with open(self._events_path, "a", encoding="utf-8") as fh:
            fh.write(event.to_json() + "\n")

    def _maybe_rotate(self) -> None:
        if not self._events_path.exists():
            return
        if self._events_path.stat().st_size > _MAX_SIZE_BYTES:
            self._rotated_path.unlink(missing_ok=True)
            self._events_path.rename(self._rotated_path)

    # ── Storage layout ────────────────────────────────────────────────────────

    def _event_files(self) -> list[Path]:
        """Every file holding events, NEWEST FIRST.

        Rotation must not blind the readers. A flight recorder whose history
        vanishes at the rotation boundary is empty exactly when it matters
        most — an incident just after a rotation — and it does not look empty,
        it looks like nothing happened.
        """
        return [p for p in (self._events_path, self._rotated_path) if p.exists()]

    @staticmethod
    def _read_lines(path: Path) -> list[str]:
        try:
            return path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []

    # ── Reading ───────────────────────────────────────────────────────────────

    def read_events(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 500,
        event_type: EventType | None = None,
    ) -> list[BlackboxEvent]:
        """Read and filter events from the JSONL stream.

        Parameters
        ----------
        since      : Only events at or after this timestamp (UTC).
        until      : Only events at or before this timestamp (UTC).
        limit      : Max number of events to return (newest first).
        event_type : Filter to a specific event type.

        Scans newest-first across the live stream and the rotated generation,
        stopping as soon as ``limit`` MATCHING events are found. It used to
        look at only the last ``limit * 4`` lines "for efficiency", which
        silently truncated every FILTERED query: the crash handler asks for the
        last 10 COMMAND events, and against a realistic mix (one command, then
        a burst of its output) 40 lines yielded 4 — or none, reported as an
        empty list. The window also bought almost nothing, because the whole
        file is read into memory by ``read_text`` either way and the loop
        already stops at ``limit``.
        """
        events: list[BlackboxEvent] = []

        for path in self._event_files():
            for line in reversed(self._read_lines(path)):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    event = BlackboxEvent.from_dict(data)
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue

                if since and event.timestamp < since:
                    continue
                if until and event.timestamp > until:
                    continue
                if event_type and event.event_type != event_type:
                    continue

                events.append(event)
                if len(events) >= limit:
                    return events  # already newest-first

        return events  # already newest-first from reversed iteration

    def tail(self, n: int = 50) -> list[BlackboxEvent]:
        """Return the last *n* events."""
        return self.read_events(limit=n)

    def event_count(self) -> int:
        """Approximate number of events (line count) across every event file.

        Counts what ``read_events`` can actually return. Counting only the live
        stream made ``navig blackbox status`` report a handful of events the
        instant a rotation happened, which reads as "nothing has been recorded"
        rather than "the history moved".
        """
        total = 0
        for path in self._event_files():
            try:
                with open(path, "rb") as fh:
                    total += sum(1 for _ in fh)
            except OSError:
                continue
        return total

    def last_event_ts(self) -> datetime | None:
        """Timestamp of the most recent event."""
        events = self.tail(1)
        return events[0].timestamp if events else None

    # ── Maintenance ───────────────────────────────────────────────────────────

    def clear(self) -> None:
        """Drop every recorded event, including the rotated generation.

        The rotated file has to go too: it is readable (see ``_event_files``),
        so leaving it behind would make "clear" a no-op for most of the
        history — and for a tool that records commands and output, data the
        user asked to delete surviving on disk is the wrong kind of surprise.
        """
        if self._events_path.exists():
            self._events_path.write_bytes(b"")
        self._rotated_path.unlink(missing_ok=True)

    def file_size_mb(self) -> float:
        """On-disk size of every event file, in MB."""
        total = 0
        for path in self._event_files():
            try:
                total += path.stat().st_size
            except OSError:
                continue
        return total / (1024 * 1024)


# ── Module-level singleton ────────────────────────────────────────────────────

_recorder: BlackboxRecorder | None = None


def get_recorder(blackbox_dir: Path | None = None) -> BlackboxRecorder:
    """Return the process recorder, or one bound to an explicit directory.

    ``get_recorder(X)`` where X differs from the singleton's directory returns
    a recorder for **X**, uncached. It used to return the singleton and drop
    the argument, so ``create_bundle(blackbox_dir=X)`` read its events from
    whichever directory happened to be initialised first while reading its
    crash reports from X — two halves of one support bundle describing
    different installs, with nothing to indicate it. The singleton itself is
    left alone, so a later argument-less call still gets the process recorder.
    """
    global _recorder
    if blackbox_dir is not None and _recorder is not None:
        if _recorder.blackbox_dir != blackbox_dir:
            return BlackboxRecorder(blackbox_dir)
    if _recorder is None:
        if blackbox_dir is None:
            from navig_blackbox._compat import blackbox_dir as _bbdir

            blackbox_dir = _bbdir()
        _recorder = BlackboxRecorder(blackbox_dir)
    return _recorder
