"""Run log files — where they live and how they are read back safely.

Event payloads are client-controlled, so ``log_path`` is never trusted: a path is
only accepted (at ingest) and only served (at read) when it resolves inside the
monitor's own log directory. Content is redacted on write by the runner and again
on read, and reads are capped so a runaway log cannot bloat a response.
"""

from __future__ import annotations

from pathlib import Path

from navig_agentctl.redact import redact

TAIL_BYTES = 64 * 1024
TAIL_LINES = 200


def log_root() -> Path:
    from navig.platform.paths import log_dir

    return log_dir() / "agent-runs"


def safe_log_path(value: object) -> Path | None:
    """Resolve *value* and return it only if it sits inside :func:`log_root`."""
    if not isinstance(value, str) or not value or len(value) > 400:
        return None
    try:
        root = log_root().resolve()
        path = Path(value).resolve()
        path.relative_to(root)
    except (ValueError, OSError):
        return None
    return path


def tail(value: object, *, lines: int = TAIL_LINES) -> list[str]:
    """Last *lines* lines of a run log, redacted. Empty when missing or outside the root."""
    path = safe_log_path(value)
    if path is None or not path.is_file():
        return []
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > TAIL_BYTES:
                fh.seek(size - TAIL_BYTES)
            raw = fh.read()
    except OSError:
        return []
    text = raw.decode("utf-8", errors="replace")
    return [redact(line, limit=500) for line in text.splitlines()[-max(1, min(lines, TAIL_LINES)):]]
