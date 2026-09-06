"""Rollup of the most recent real claim run — the Status tab's at-a-glance health
view for unattended (scheduled) claiming. One small JSON file next to the ledger.

Only *real* runs are recorded (dry-runs are tests and never overwrite it), so
"Last claim" always answers "did my auto-claim actually run, and what happened?".
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def _path() -> Path:
    try:
        from navig.platform.paths import config_dir

        base = config_dir()
    except Exception:  # noqa: BLE001
        base = Path.home() / ".navig"
    d = base / "games"
    d.mkdir(parents=True, exist_ok=True)
    return d / "last_run.json"


def record(summary: dict) -> None:
    """Persist a rollup of a completed real claim run. Best-effort — never raises.

    ``summary`` is a ``runner.run_claim`` result dict (``checked``/``attempted``/
    ``claimed``/``login``/``results``/``message``). Dry-runs are ignored so the
    health view reflects only real (scheduled or confirmed) claims.
    """
    try:
        if bool(summary.get("dry_run", False)):
            return
        results = summary.get("results") or []
        needs_manual = sum(
            1 for r in results if isinstance(r, dict) and r.get("status") == "needs_manual"
        )
        data = {
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "checked": int(summary.get("checked", 0) or 0),
            "attempted": int(summary.get("attempted", 0) or 0),
            "claimed": int(summary.get("claimed", 0) or 0),
            "needs_manual": needs_manual,
            "login": summary.get("login"),
            "message": summary.get("message"),
        }
        # Atomic, like the ledger and deals state: the claim SUBPROCESS writes this
        # while the daemon's /games/status route reads it. A plain write_text can be
        # read half-finished (→ the Status tab silently claims "no last run") and a
        # crash mid-write would truncate the file permanently.
        path = _path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception:  # noqa: BLE001 — telemetry, never a blocker
        pass


def read() -> "dict | None":
    """The last recorded real claim run, or None if none yet."""
    try:
        p = _path()
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def login_needs_signin() -> bool:
    """Did the last *real* auto-claim fail to sign in to Epic?

    Derived from the rollup the scheduled claim already writes (``login`` ==
    ``needs_manual`` means ``run_epic_claims`` never authenticated), so status
    surfaces can show "your last auto-claim couldn't sign in — re-authorize"
    **for free** — no browser probe. It reflects the last run, not this instant:
    honest and timestamped (``finished_at``), and it self-corrects on the next
    successful claim. One definition, shared by the CLI and the deck route.
    """
    r = read()
    return bool(r and r.get("login") == "needs_manual")
