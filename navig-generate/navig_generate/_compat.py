"""The few navig services the media engine uses, with a standalone twin when navig is absent."""

from __future__ import annotations

import logging
from typing import Any

# One owner for "which space am I in": the media library. Re-exported so
# ``navig_generate._compat.active_working_dir`` keeps working for existing callers.
from navig_media_core._compat import _resolved, active_working_dir  # noqa: F401


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
