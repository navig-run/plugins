"""Subtitle time stamps — the one formatter for SRT and ASS (ADR-0002).

Both formats are computed from a whole number of milliseconds (SRT) or centiseconds (ASS)
and only then split into fields. Formatting the fractional part on its own rounds 1.9996 s up
to ``01,1000`` (SRT) or 59.996 s to ``:60.00`` (ASS) — stamps players reject or mis-read.
"""

from __future__ import annotations


def srt_timestamp(seconds: float) -> str:
    """``HH:MM:SS,mmm`` — the SRT time format, comma decimal separator included."""
    if seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def ass_timestamp(seconds: float) -> str:
    """``H:MM:SS.cc`` — the ASS/SSA time format (centiseconds)."""
    if seconds < 0:
        seconds = 0.0
    total_cs = int(round(seconds * 100))
    hours, remainder = divmod(total_cs, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    secs, centis = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"
