"""The voice usage ledger — what read-aloud spent, per day and provider.

Paid speech is billed per character, so the daily budget (``voice.daily_char_budget``) is
enforced against this ledger *before* a paid request is made, and every paid request that
actually reached a provider is recorded *after* it succeeds. A cache hit re-buys nothing and
is never recorded.

Storage: one small JSON file (``<config>/voice/usage.json``), written atomically (temp file +
``os.replace``) under a process lock; the daemon is its only writer. The last
:data:`KEEP_DAYS` days are kept. A missing or unreadable file reads as "nothing spent" for
the *read* path only — a write never replaces an unreadable ledger (it is quarantined to
``usage.json.bad`` first), so a transient read failure can never wipe history.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import threading
from pathlib import Path
from typing import Any

from navig_sdk.host import config_dir

KEEP_DAYS = 62
_LOCK = threading.Lock()


def ledger_path() -> Path:
    return config_dir() / "voice" / "usage.json"


def _today() -> str:
    return _dt.date.today().isoformat()


def _read(path: Path) -> dict[str, Any] | None:
    """The ledger, ``{}`` when absent, ``None`` when present but unreadable."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def record(provider: str, chars: int, *, usd: float | None = None, day: str | None = None) -> None:
    """Add one paid request to today's row for ``provider``."""
    if chars <= 0:
        return
    path = ledger_path()
    with _LOCK:
        data = _read(path)
        if data is None:  # unreadable: keep it for inspection, start a fresh ledger
            try:
                os.replace(path, path.with_suffix(".json.bad"))
            except OSError:
                pass
            data = {}
        days = data.setdefault("days", {})
        row = days.setdefault(day or _today(), {}).setdefault(provider, {"chars": 0, "requests": 0, "usd": 0.0})
        row["chars"] += int(chars)
        row["requests"] += 1
        if usd:
            row["usd"] = round(row.get("usd", 0.0) + float(usd), 6)
        for old in sorted(days)[:-KEEP_DAYS]:
            days.pop(old, None)
        _write(path, data)


def day_rows(day: str | None = None) -> dict[str, dict[str, Any]]:
    """``{provider: {chars, requests, usd}}`` for one day (default today)."""
    data = _read(ledger_path()) or {}
    return dict(data.get("days", {}).get(day or _today(), {}))


def spent_today() -> int:
    """Paid characters spent today, all providers together."""
    return sum(int(r.get("chars", 0)) for r in day_rows().values())


def recent(days: int = 7) -> list[dict[str, Any]]:
    """The last ``days`` days, newest first: ``[{day, chars, requests, usd, providers}]``."""
    data = _read(ledger_path()) or {}
    out = []
    for day in sorted(data.get("days", {}), reverse=True)[:days]:
        rows = data["days"][day]
        out.append({
            "day": day,
            "chars": sum(int(r.get("chars", 0)) for r in rows.values()),
            "requests": sum(int(r.get("requests", 0)) for r in rows.values()),
            "usd": round(sum(float(r.get("usd", 0.0)) for r in rows.values()), 4),
            "providers": rows,
        })
    return out
