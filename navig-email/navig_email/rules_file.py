"""The space's ``mailroom/rules.yaml`` — the operator-owned rule set.

The daemon keeps its own rules in ``~/.navig/email/config.json`` (edited from the
Deck); the mailroom keeps them in the space, in a file a person can read and diff.
``load_rules`` returns the space file's rules normalised, followed by any enabled
daemon rules (so nothing already configured is silently ignored).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import config as daemon_cfg
from .rules import normalize, validate
from .space import MailroomPaths


def read_rules_yaml(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """``(rules, meta)`` from a rules.yaml; ``([], {})`` when the file is absent."""
    if not path.exists():
        return [], {}
    from navig_sdk.files import safe_load_yaml

    data = safe_load_yaml(path)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping with a `rules:` list")
    rules = data.get("rules") or []
    if not isinstance(rules, list):
        raise ValueError(f"{path}: `rules:` must be a list")
    out = []
    for i, r in enumerate(rules):
        if not isinstance(r, dict):
            raise ValueError(f"{path}: rule #{i + 1} is not a mapping")
        n = normalize(r)
        n.setdefault("id", n.get("name") or f"rule-{i + 1}")
        out.append(n)
    meta = {k: v for k, v in data.items() if k != "rules"}
    return out, meta


def load_rules(
    paths: MailroomPaths, *, include_daemon: bool = True
) -> list[dict[str, Any]]:
    rules, _meta = read_rules_yaml(paths.rules_yaml)
    if include_daemon:
        try:
            for r in daemon_cfg.load_config().get("rules") or []:
                if r.get("enabled", True):
                    n = normalize(r)
                    n.setdefault("id", n.get("name") or "daemon-rule")
                    n["source"] = "daemon"
                    rules.append(n)
        except Exception:  # noqa: BLE001 — the daemon store is optional here
            pass
    return [r for r in rules if r.get("enabled", True)]


def check_rules(paths: MailroomPaths) -> list[str]:
    rules, _ = read_rules_yaml(paths.rules_yaml)
    return validate(rules)
