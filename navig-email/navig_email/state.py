"""Per-space watch state: ``<space>/.navig/email/state.json``.

Tiny and boring on purpose. ``history_id`` is the Gmail cursor for incremental sync;
``seen_ids`` (capped) is the belt-and-braces guard against acting twice on one
message when the cursor has to be rebuilt from a time-window search; ``seeded``
records that the first run has happened (it never notifies the backlog).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from navig_sdk.files import atomic_write_json, load_json_safe

from .space import MailroomPaths

SEEN_CAP = 500

_DEFAULT: dict[str, Any] = {
    "account": "",
    "history_id": "",
    "seen_ids": [],
    "seeded": False,
    "last_watch": "",
    "last_error": "",
    "label_ids": {},  # label name → id cache, refreshed when a name is missing
}


def load(paths: MailroomPaths) -> dict[str, Any]:
    data = load_json_safe(paths.state_json, default=_DEFAULT)
    out = dict(_DEFAULT)
    if isinstance(data, dict):
        out.update({k: data.get(k, out[k]) for k in out})
    out["seen_ids"] = list(out.get("seen_ids") or [])[:SEEN_CAP]
    out["label_ids"] = dict(out.get("label_ids") or {})
    return out


def save(paths: MailroomPaths, state: dict[str, Any]) -> None:
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    state = dict(state)
    state["seen_ids"] = list(state.get("seen_ids") or [])[:SEEN_CAP]
    atomic_write_json(state, paths.state_json)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def remember(state: dict[str, Any], ids: list[str]) -> None:
    seen = [i for i in ids if i] + [
        i for i in state.get("seen_ids") or [] if i not in set(ids)
    ]
    state["seen_ids"] = seen[:SEEN_CAP]
