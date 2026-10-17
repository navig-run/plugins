"""Where a channel workspace lives — texts, plates, voice, beds, outputs.

Same contract as navig-text's workspace helper (the channel is one directory, both
plugins address it the same way): ``--workspace/-C`` → env ``NAVIG_WORKSPACE`` /
``NAVIG_VIDEO_WORKSPACE`` → the default channel directory. Media never enters the
navig repo — plugins read and write HERE.

ffmpeg is probed on PATH (never a pip dep, per the plugin spec): :func:`ffmpeg`
returns the binary or ``None``, and the verbs that need it turn ``None`` into
``typer.Exit(1)`` with an install hint.
"""

from __future__ import annotations

import importlib
import os
import shutil
import sys
from pathlib import Path
from typing import Optional

#: The default channel workspace (owner directive, 2026-10-13).
DEFAULT_WORKSPACE = Path(r"C:\studio\schema\promo\wave1")


def default_workspace(explicit: Optional[Path] = None) -> Path:
    """The workspace a command should act on: flag → env → the channel default."""
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get("NAVIG_WORKSPACE") or os.environ.get("NAVIG_VIDEO_WORKSPACE")
    if env:
        return Path(env)
    return DEFAULT_WORKSPACE


def attach_workspace(ws: Path) -> Path:
    """Put ``ws`` first on ``sys.path`` and drop workspace modules cached elsewhere."""
    ws = Path(ws).resolve()
    for name in ("episodes", "transmission", "build", "live", "statement", "formats"):
        mod = sys.modules.get(name)
        if mod is None:
            continue
        origin = getattr(mod, "__file__", None)
        try:
            if not origin or Path(origin).parent.resolve() != ws:
                sys.modules.pop(name, None)
        except OSError:
            sys.modules.pop(name, None)
    if str(ws) not in sys.path:
        sys.path.insert(0, str(ws))
    return ws


def load_workspace_module(ws: Path, name: str):
    """Import ``name`` (e.g. ``episodes``) from the workspace at ``ws``."""
    attach_workspace(ws)
    return importlib.import_module(name)


def ffmpeg() -> Optional[str]:
    """The ffmpeg binary on PATH, or None. Never a pip dependency."""
    return shutil.which("ffmpeg")


def ffprobe() -> Optional[str]:
    """The ffprobe binary on PATH, or None."""
    return shutil.which("ffprobe")


def require_ffmpeg() -> str:
    """ffmpeg or a usage-class exit — render verbs call this first."""
    import typer

    from navig_sdk import console as ch

    bin_ = ffmpeg()
    if not bin_:
        ch.error("ffmpeg not found on PATH", "Install ffmpeg (winget install ffmpeg / apt install ffmpeg) and retry")
        raise typer.Exit(1)
    return bin_
