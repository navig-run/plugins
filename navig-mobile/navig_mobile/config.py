"""navig-mobile config + path helpers.

Thin wrappers over ``navig.config`` (plugin-scoped settings persisted under
``plugins.mobile.*``) with a standalone fallback so the plugin does not hard-fail
when navig-core isn't importable. Also resolves the data directory used for the
device inventory DB, backups, and case files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_PLUGIN = "mobile"


def get(key: str, default: Any = None) -> Any:
    """``plugins.mobile.<key>`` — navig's config inside navig, the same file on its own."""
    try:
        from navig_sdk import settings

        return settings.get(_PLUGIN, key, default)
    except Exception:
        return default


def set(key: str, value: Any) -> None:
    """Persist ``plugins.mobile.<key>``. Standalone this used to be silently discarded."""
    from navig_sdk import settings

    settings.set(_PLUGIN, key, value)


def data_dir() -> Path:
    """navig's data dir (honours ``NAVIG_DATA_DIR`` and the system-service location)."""
    from navig_sdk.host import data_dir as _data_dir

    base = Path(_data_dir())
    base.mkdir(parents=True, exist_ok=True)
    return base


def mobile_dir() -> Path:
    """Root for navig-mobile artifacts (backups, cases)."""
    d = data_dir() / "mobile"
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path() -> Path:
    return data_dir() / "mobile.db"


def operator_email() -> str:
    """Best-effort operator identity for evidence manifests / consent records."""
    for key in ("user.email", "operator.email", "founder.email"):
        val = get(key) or _global(key)
        if val:
            return str(val)
    import os

    return os.environ.get("NAVIG_OPERATOR_EMAIL", "") or ""


def _global(key: str) -> Any:
    try:
        from navig.config import get_config_manager

        node: Any = get_config_manager().global_config
        for part in key.split("."):
            if not isinstance(node, dict):
                return None
            node = node.get(part)
        return node
    except Exception:
        return None
