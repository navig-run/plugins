"""Where a channel workspace lives — mark, plates, specs, outputs.

The same contract as the other channel-facing plugins: ``--workspace/-C`` → env
``NAVIG_WORKSPACE`` / ``NAVIG_GENERATE_WORKSPACE`` → the default channel
directory. Only the rendering code lives in this plugin; the mark and every
spec stay with the channel.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

#: The default channel workspace (owner directive, 2026-10-13).
DEFAULT_WORKSPACE = Path(r"C:\studio\schema\promo\wave1")


def default_workspace(explicit: Optional[Path] = None) -> Path:
    """The workspace a command should act on: flag → env → the channel default."""
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get("NAVIG_WORKSPACE") or os.environ.get("NAVIG_GENERATE_WORKSPACE")
    if env:
        return Path(env)
    return DEFAULT_WORKSPACE
