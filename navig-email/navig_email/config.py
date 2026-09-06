"""Email-ops config — filter rules + briefing schedules, persisted as JSON at
``~/.navig/email/config.json``. Best-effort; a missing/corrupt file yields the
empty defaults so the service never crashes."""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any

from navig.core.json_io import (
    JsonReadError,
    atomic_write_json,
    load_json_for_update,
    load_json_safe,
)
from navig.platform import paths

logger = logging.getLogger("navig_email")


def _config_path() -> Path:
    return paths.data_dir().parent / "email" / "config.json"  # ~/.navig/email/config.json


_DEFAULT: dict[str, Any] = {
    "monitor_enabled": True,
    "rules": [],       # [{id,name,from,subject_contains,subject_exact,body_words[],channels[],enabled}]
    "briefings": [],   # [{id,name,query|label,cadence,hour,weekday,day,channels[],focus,enabled}]
    "state": {"seen_ids": [], "last_brief": {}},
}


def _merge_defaults(data: Any) -> dict[str, Any]:
    """Overlay *data* onto a fresh copy of the defaults (forward-compat)."""
    out = json.loads(json.dumps(_DEFAULT))
    if isinstance(data, dict):
        out.update({k: data.get(k, out[k]) for k in out})
        out["state"] = {**_DEFAULT["state"], **(data.get("state") or {})}
    return out


def load_config() -> dict[str, Any]:
    """Read-only view of the config — degrades to defaults on any failure, never raises,
    never writes back. Use :func:`load_config_for_update` for a read-modify-write."""
    return _merge_defaults(load_json_safe(_config_path(), default=_DEFAULT))


def load_config_for_update() -> dict[str, Any]:
    """Load for a read-MODIFY-write. Raises :class:`JsonReadError` if the file exists but
    is transiently unreadable (a lock), so the caller ABORTS the save instead of writing
    empty defaults over every rule and briefing. Corrupt / wrong-shape files are quarantined.
    """
    return _merge_defaults(load_json_for_update(_config_path(), default=_DEFAULT))


def save_config(cfg: dict[str, Any]) -> dict[str, Any]:
    p = _config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # normalise ids on rules/briefings
    for coll in ("rules", "briefings"):
        for item in cfg.get(coll, []) or []:
            if not item.get("id"):
                item["id"] = uuid.uuid4().hex[:8]
    atomic_write_json(cfg, p)  # temp+fsync+atomic replace — a crash can't truncate the store
    return cfg


def get_state() -> dict[str, Any]:
    return load_config().get("state") or {"seen_ids": [], "last_brief": {}}


def update_state(**changes: Any) -> None:
    """Merge *changes* into the persisted ``state`` block. Called on every unattended
    ``EmailService.tick()`` — so if the file is transiently unreadable we SKIP the write
    rather than persist empty defaults over the user's rules and briefings."""
    try:
        cfg = load_config_for_update()
    except JsonReadError:
        logger.warning(
            "email config unreadable right now — skipping state update to avoid wiping "
            "rules/briefings (will retry next tick)"
        )
        return
    cfg.setdefault("state", {})
    cfg["state"].update(changes)
    save_config(cfg)
