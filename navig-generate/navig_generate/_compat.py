"""The few navig services the media engine uses, with a standalone twin when navig is absent."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any


def get_logger(subsystem: str) -> Any:
    """navig's structured logger ``navig.<subsystem>`` when installed, else a plain one of that name.

    The name stays in navig's hierarchy either way, so inside navig these modules keep writing
    to navig's debug log exactly as they did before they moved.
    """
    try:
        from navig.core.logging import get_logger as _impl  # noqa: PLC0415
    except ImportError:
        return logging.getLogger(f"navig.{subsystem}")
    return _impl(subsystem)


def active_working_dir() -> Path:
    """The space to act on: navig's active space, or standalone the project you stand in.

    Without navig there is no pinned/active space, so it walks up from the current directory
    to the nearest ``.navig/`` (the same git-style rule navig's CLI applies), else the cwd.
    The home ``~/.navig`` and the global config dir are navig's GLOBAL layer, never a project,
    so they are skipped exactly as ``navig.spaces.resolver`` does — otherwise every folder
    under home would file its variants under the home directory.
    """
    try:
        from navig.spaces.active import get_active_working_dir  # noqa: PLC0415
    except ImportError:
        from navig_sdk.host import config_dir  # noqa: PLC0415

        cur = Path(os.environ.get("NAVIG_INVOCATION_CWD") or Path.cwd())
        global_layer = {_resolved(Path.home() / ".navig"), _resolved(config_dir())}
        for d in (cur, *cur.parents):
            try:
                navig_dir = d / ".navig"
                if navig_dir.is_dir() and _resolved(navig_dir) not in global_layer:
                    return d
            except OSError:
                break
        return cur
    return get_active_working_dir()


def _resolved(p: Path) -> Path:
    try:
        return p.resolve()
    except OSError:
        return p


def global_config() -> dict[str, Any]:
    """navig's global config (``<config dir>/config.yaml``): the live one, or the file itself."""
    try:
        from navig.config import get_config_manager  # noqa: PLC0415
    except ImportError:
        from navig_sdk.files import safe_load_yaml  # noqa: PLC0415
        from navig_sdk.host import config_dir  # noqa: PLC0415

        data = safe_load_yaml(config_dir() / "config.yaml")
        return data if isinstance(data, dict) else {}
    return get_config_manager().global_config
