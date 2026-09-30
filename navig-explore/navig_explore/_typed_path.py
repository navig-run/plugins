"""Resolve a path the operator TYPED against the directory they typed it in.

``navig`` chdir's into the active space before a command runs, so a relative path
resolved with a bare ``Path(x)`` lands inside the space, not where the operator stands.
Core's ``navig.platform.paths.resolve_user_path`` fixes that; this shim uses it when the
installed core has it and otherwise reads the same ``NAVIG_INVOCATION_CWD`` variable, so
the plugin keeps working against an older core (and standalone, where cwd is correct).
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from navig.platform.paths import resolve_user_path
except ImportError:  # core older than the helper

    def resolve_user_path(given: Path | str) -> Path:
        p = Path(given).expanduser()
        if p.is_absolute():
            return p.resolve()
        origin = os.environ.get("NAVIG_INVOCATION_CWD")
        base = Path(origin) if origin and Path(origin).is_dir() else Path.cwd()
        return (base / p).resolve()


__all__ = ["resolve_user_path"]
