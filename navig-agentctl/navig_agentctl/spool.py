"""Offline spool — events a producer could not deliver because the daemon was down.

Stdlib only (the Claude Code hook imports this and must stay fast and dependency
free). A producer appends one JSON line per event; the daemon's sweeper drains the
file into the run ledger. Events carry their own ``event_id``, so a drain that is
interrupted and replayed is harmless.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

MAX_SPOOL_BYTES = 2 * 1024 * 1024  # a dead daemon must not grow this without bound
MAX_DRAIN_EVENTS = 2000


def config_dir() -> Path:
    """Mirror of ``navig.platform.paths.config_dir`` (no navig import: the hook is cold-start)."""
    env = os.environ.get("NAVIG_CONFIG_DIR")
    return Path(env) if env else Path.home() / ".navig"


def spool_path() -> Path:
    return config_dir() / "agent-monitor" / "spool.jsonl"


def append(event: dict) -> bool:
    """Queue *event* for later delivery. Never raises; False when dropped."""
    try:
        path = spool_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > MAX_SPOOL_BYTES:
            return False
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, separators=(",", ":")) + "\n")
        return True
    except OSError:
        return False


def drain() -> list[dict]:
    """Atomically claim the spool and return its events (oldest first)."""
    path = spool_path()
    claimed = path.with_suffix(".draining")
    try:
        if claimed.exists():  # a previous drain died mid-way: finish it first
            pass
        elif path.exists():
            os.replace(path, claimed)
        else:
            return []
        text = claimed.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    batch, rest = lines[:MAX_DRAIN_EVENTS], lines[MAX_DRAIN_EVENTS:]
    events: list[dict] = []
    for line in batch:
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    try:
        if rest:
            # Keep what this pass did not take: the next drain finishes the claimed
            # file first. (Unlinking it dropped every event past the cap — measured.)
            claimed.write_text("\n".join(rest) + "\n", encoding="utf-8")
        else:
            claimed.unlink()
    except OSError:
        pass
    return events
