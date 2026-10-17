"""Where a channel workspace lives — texts, scripts and assets a channel renders from.

A workspace is NOT part of this plugin: media and copy stay where they are (the owner's
channel directory), and only the code that reads them lives here. Resolution order:

  1. an explicit ``--workspace/-C`` on the command,
  2. the ``NAVIG_WORKSPACE`` (or ``NAVIG_VIDEO_WORKSPACE``) environment variable,
  3. the default channel workspace this plugin was built for.

:func:`load_workspace_module` imports a Python module FROM the workspace (the channel's
own ``episodes``, ``transmission``, ``live`` … data scripts) so commands can read the
current values without copying them anywhere. Modules cached from a DIFFERENT workspace
are dropped first — a test that runs two fixture workspaces in one process must not see
the first one's data.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from typing import Optional

#: The default channel workspace (owner directive, 2026-10-13).
DEFAULT_WORKSPACE = Path(r"C:\studio\schema\promo\wave1")

#: Modules a channel workspace owns that a command may need to import. Purged before a
#: load when they were cached from another workspace — ``build`` is a real PyPI package,
#: so a stale one would silently satisfy ``import build`` with the WRONG module.
WORKSPACE_MODULES = frozenset(
    {
        "episodes",
        "transmission",
        "lore_texts",
        "publish_kit",
        "post_kit",
        "build",
        "live",
        "speech",
        "say",
        "music",
        "beatgrid",
        "formats",
        "statement",
        "story_kit",
        "rollcall",
        "card",
        "cards_motion",
        "accents",
        "weekly",
        "telegram_post",
        "key_drop",
    }
)


def default_workspace(explicit: Optional[Path] = None) -> Path:
    """The workspace a command should act on: flag → env → the channel default."""
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get("NAVIG_WORKSPACE") or os.environ.get("NAVIG_VIDEO_WORKSPACE")
    if env:
        return Path(env)
    return DEFAULT_WORKSPACE


#: Directories attach_workspace has itself put on sys.path (pruned on the next
#: attach — see below).
_ATTACHED: list[str] = []


def attach_workspace(ws: Path) -> Path:
    """Put ``ws`` first on ``sys.path`` and drop cached workspace modules.

    Cached means stale here: the sync WRITES these files, so a second run in the
    same process (tests, an API caller) must re-read the file as it is now — the
    original script got a fresh interpreter per run and never faced this.

    Directories attached EARLIER are pruned for the same reason: a module that
    does not exist in this workspace must fail to import, not silently resolve
    from the previous fixture's directory still sitting on ``sys.path``.
    """
    global _ATTACHED
    ws = Path(ws).resolve()
    for name in WORKSPACE_MODULES:
        if name in sys.modules:
            sys.modules.pop(name, None)
    for old in _ATTACHED:
        if old != str(ws) and old in sys.path:
            sys.path.remove(old)
    if str(ws) not in sys.path:
        sys.path.insert(0, str(ws))
    _ATTACHED = [str(ws)]
    return ws


def load_workspace_module(ws: Path, name: str):
    """Import ``name`` (e.g. ``episodes``) from the workspace at ``ws``, freshly."""
    attach_workspace(ws)
    return importlib.import_module(name)


def channel_config(ws: Path) -> dict:
    """``<ws>/channel.json`` — workspace-declared master path, sweep rules, endpoints.

    Absent file = all defaults; a malformed file is a usage error the caller reports.
    """
    import json

    cfg_path = Path(ws) / "channel.json"
    if not cfg_path.is_file():
        return {}
    return json.loads(cfg_path.read_text(encoding="utf-8"))


def resolve_master(ws: Path, explicit: Optional[Path] = None) -> Path:
    """The master text file: ``--master`` flag → ``channel.json`` → newest ALL-TEXTS*.md."""
    if explicit is not None:
        return Path(explicit)
    cfg = channel_config(ws)
    if cfg.get("master"):
        return Path(ws) / str(cfg["master"])
    candidates = sorted((Path(ws) / "out").glob("ALL-TEXTS*.md"))
    if not candidates:
        return Path(ws) / "out" / "ALL-TEXTS.md"
    # ALL-TEXTS-rev10 sorts before rev9 lexicographically — sort on the revision number.
    def rev(p: Path) -> int:
        tail = p.stem.rsplit("-rev", 1)
        return int(tail[1]) if len(tail) == 2 else -1

    return max(candidates, key=rev)
