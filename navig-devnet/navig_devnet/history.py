"""Per-space request history for the HTTP repeater.

Each sent request is appended as one JSON line under the active space's
``.navig/devnet/http_history.jsonl`` — so history (and the ``diff`` between two
responses) is scoped to the workspace you are working in, like the module itself.
Standalone (no navig) it falls back to the current directory's ``.navig``.

Best-effort throughout: a history failure must never fail the request that was
already sent — the send is the product, the log is a convenience.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

#: Response body kept in history for diffing (smaller than the live preview cap).
_HIST_BODY_CAP = 64 * 1024


def _history_dir() -> Path:
    try:
        from navig.spaces.active import get_active_working_dir  # noqa: PLC0415

        base = get_active_working_dir()
    except Exception:  # noqa: BLE001 — standalone / navig absent
        base = Path.cwd()
    d = base / ".navig" / "devnet"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _history_file() -> Path:
    return _history_dir() / "http_history.jsonl"


def record(result: dict[str, Any]) -> int | None:
    """Append a repeater *result* to history; return its id (1-based), or None on
    failure. The id is the new line number, so ``diff 3 7`` reads naturally."""
    try:
        f = _history_file()
        existing = 0
        if f.exists():
            with f.open("r", encoding="utf-8") as fh:
                existing = sum(1 for _ in fh)
        entry = {
            "id": existing + 1,
            "ts": time.time(),
            "method": result["request"]["method"],
            "url": result["request"]["url"],
            "status": result["response"]["status"],
            "elapsed_ms": result.get("elapsed_ms"),
            "body_len": result["response"]["body_len"],
            "request_headers": result["request"]["headers"],
            "response_headers": result["response"]["headers"],
            "response_body": result["response"]["body"][:_HIST_BODY_CAP],
            "final_url": result["response"]["final_url"],
        }
        with f.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        return entry["id"]
    except Exception:  # noqa: BLE001 — never fail the request over its log
        return None


def load(limit: int = 50) -> list[dict[str, Any]]:
    """Return the most recent *limit* history entries (newest last)."""
    f = _history_file()
    if not f.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with f.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue  # skip a corrupt line, keep the rest
    except OSError:
        return []
    return out[-limit:] if limit else out


def get(entry_id: int) -> dict[str, Any] | None:
    """Return the history entry with *entry_id*, or None if absent."""
    for e in load(limit=0):
        if e.get("id") == entry_id:
            return e
    return None
