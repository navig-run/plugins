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
    try:
        from navig.config import get_config_manager

        return get_config_manager().get_plugin_config(_PLUGIN, key, default)
    except Exception:
        return default


def set(key: str, value: Any) -> None:
    try:
        from navig.config import get_config_manager

        get_config_manager().set_plugin_config(_PLUGIN, key, value)
    except Exception:
        pass


def data_dir() -> Path:
    """NAVIG data dir (honors ``NAVIG_DATA_DIR``); falls back to ~/.navig/data."""
    try:
        from navig.platform import paths

        return Path(paths.data_dir())
    except Exception:
        import os

        env = os.environ.get("NAVIG_DATA_DIR")
        base = Path(env) if env else (Path.home() / ".navig" / "data")
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
