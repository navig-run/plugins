"""Where a channel workspace lives — texts, voice cache, beds, grids.

The same contract as the other channel-facing plugins: ``--workspace/-C`` →
env ``NAVIG_WORKSPACE`` / ``NAVIG_AUDIO_WORKSPACE`` → the default channel
directory. Audio state that belongs to the channel (the voice cache, the bed
store, the beat grids) lives THERE — only the code lives in this plugin.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from typing import Optional

#: The default channel workspace (owner directive, 2026-10-13).
DEFAULT_WORKSPACE = Path(r"C:\studio\schema\promo\wave1")

#: Workspace-owned modules a command may need to import (episode data etc.).
WORKSPACE_MODULES = frozenset({"episodes", "transmission", "build", "live", "speech", "say", "music", "beatgrid"})


def default_workspace(explicit: Optional[Path] = None) -> Path:
    """The workspace a command should act on: flag → env → the channel default."""
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get("NAVIG_WORKSPACE") or os.environ.get("NAVIG_AUDIO_WORKSPACE")
    if env:
        return Path(env)
    return DEFAULT_WORKSPACE


def load_workspace_module(ws: Path, name: str):
    """Import ``name`` (e.g. ``episodes``) from the workspace at ``ws``, freshly.

    Workspace modules are purged first: the channel scripts write these files,
    so a second load in one process must see the file as it is now.
    """
    ws = Path(ws).resolve()
    for mod in WORKSPACE_MODULES:
        if mod in sys.modules:
            sys.modules.pop(mod, None)
    if str(ws) not in sys.path:
        sys.path.insert(0, str(ws))
    return importlib.import_module(name)
