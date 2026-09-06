"""Scheduling — register a recurring "claim free games" job with core's
``CronService`` by writing to its persisted ``cron_jobs.json``.

The CLI runs in a *separate* process from the daemon, so we can't call the live
``gateway.cron_service`` object. Instead we edit the same file the service loads
at start (``<global_config_dir>/scheduler/cron_jobs.json``) in the documented
schema, then tell the user to (re)start the daemon to pick it up. The job command
is a plain ``navig games claim`` invocation, which CronService runs via subprocess.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from navig.core.json_io import (
    JsonReadError,
    atomic_write_json,
    load_json_for_update,
    load_json_safe,
)

# Named jobs this plugin manages (one per feature).
CLAIM_JOB = "navig-games: claim free games"
CLAIM_COMMAND = "navig games claim --all --yes"
DEALS_JOB = "navig-games: steam deals"
DEALS_COMMAND = "navig games deals notify"
UNIFY_JOB = "navig-games: unify to steam"
UNIFY_COMMAND = "navig games unify --yes"


def _jobs_path() -> Path:
    from navig.config import get_config_manager

    base = get_config_manager().global_config_dir / "scheduler"
    base.mkdir(parents=True, exist_ok=True)
    return base / "cron_jobs.json"


def _normalize(data: dict) -> dict:
    data.setdefault("counter", 0)
    if not isinstance(data.get("jobs"), list):
        data["jobs"] = []
    return data


def _load_for_update(path: Path) -> dict:
    """Load the shared cron store for a load-modify-SAVE.

    This is ``<config>/scheduler/cron_jobs.json`` — the SAME file the core CronService
    and every other plugin schedule into. A raw ``json.load`` / ``except: {}`` here
    turned a transient OS lock or a corrupt file into an EMPTY job set, and the next
    ``_save`` (one games job added/removed) then WIPED every other schedule — core
    backups, habits, other plugins. ``load_json_for_update`` instead RAISES
    ``JsonReadError`` on a lock that survives the retries, so the caller aborts the save;
    a corrupt file is quarantined ``*.corrupt`` and treated as empty.
    """
    return _normalize(load_json_for_update(path, default={"counter": 0, "jobs": []}))


def _load_safe(path: Path) -> dict:
    """Read-only load for status views: degrade to an empty store, never raise."""
    return _normalize(load_json_safe(path, default={"counter": 0, "jobs": []}))


def _save(path: Path, data: dict) -> None:
    atomic_write_json(data, path)  # temp + fsync + atomic replace, with transient-lock retry


def _next_id(data: dict) -> tuple[str, int]:
    max_n = data.get("counter", 0)
    for job in data.get("jobs", []):
        jid = str(job.get("id", ""))
        if jid.startswith("job_"):
            try:
                max_n = max(max_n, int(jid.split("_", 1)[1]))
            except (ValueError, IndexError):
                pass
    return f"job_{max_n + 1}", max_n + 1


def upsert_job(name: str, command: str, when: str = "daily", timeout: int = 900) -> dict:
    """Create/update a named recurring job. ``when`` is natural language
    ("daily" / "weekly") or a 5-field cron expression."""
    path = _jobs_path()
    try:
        data = _load_for_update(path)
    except JsonReadError as exc:
        return {"ok": False, "error": f"schedule store unreadable — refusing to overwrite it: {exc}",
                "path": str(path)}

    for job in data["jobs"]:
        if job.get("name") == name:
            job["schedule"] = when
            job["command"] = command
            job["enabled"] = True
            _save(path, data)
            return {"ok": True, "action": "updated", "schedule": when, "path": str(path)}

    jid, counter = _next_id(data)
    data["jobs"].append(
        {
            "id": jid, "name": name, "schedule": when, "command": command,
            "enabled": True, "timeout_seconds": timeout,
            "retry_count": 0, "max_retries": 2,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    data["counter"] = max(data.get("counter", 0), counter)
    _save(path, data)
    return {"ok": True, "action": "created", "schedule": when, "path": str(path)}


def remove_job(name: str) -> dict:
    path = _jobs_path()
    try:
        data = _load_for_update(path)
    except JsonReadError as exc:
        return {"ok": False, "error": f"schedule store unreadable — refusing to modify it: {exc}",
                "path": str(path)}
    before = len(data["jobs"])
    data["jobs"] = [j for j in data["jobs"] if j.get("name") != name]
    _save(path, data)
    return {"ok": True, "removed": before - len(data["jobs"]), "path": str(path)}


def job_status(name: str) -> dict:
    path = _jobs_path()
    data = _load_safe(path)
    for job in data["jobs"]:
        if job.get("name") == name:
            return {
                "enabled": bool(job.get("enabled")), "schedule": job.get("schedule"),
                "command": job.get("command"), "last_run": job.get("last_run"),
                "next_run": job.get("next_run"), "last_status": job.get("last_status"),
                "path": str(path),
            }
    return {"enabled": False, "schedule": None, "path": str(path)}


# ── convenience wrappers per feature ─────────────────────────────────────────

def enable(when: str = "daily") -> dict:
    return upsert_job(CLAIM_JOB, CLAIM_COMMAND, when)


def disable() -> dict:
    return remove_job(CLAIM_JOB)


def status() -> dict:
    return job_status(CLAIM_JOB)


def enable_deals(when: str = "daily") -> dict:
    return upsert_job(DEALS_JOB, DEALS_COMMAND, when)


def disable_deals() -> dict:
    return remove_job(DEALS_JOB)


def deals_status() -> dict:
    return job_status(DEALS_JOB)


def enable_unify(when: str = "weekly") -> dict:
    return upsert_job(UNIFY_JOB, UNIFY_COMMAND, when)


def disable_unify() -> dict:
    return remove_job(UNIFY_JOB)


def unify_status() -> dict:
    return job_status(UNIFY_JOB)
