"""The navig services the media library uses, with a standalone twin when navig is absent.

Owned here (not in navig-generate) so the library never imports a sibling plugin to find out
which space it is in. ``navig_generate._compat`` re-exports :func:`active_working_dir`.
"""

from __future__ import annotations

import os
from pathlib import Path


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
