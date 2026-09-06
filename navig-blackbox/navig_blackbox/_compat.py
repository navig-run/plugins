"""Compatibility seam — use navig's implementations when navig is installed, else fall back.

navig-blackbox installs and runs **without** navig. Every navig-internal dependency the engine
needs (console, atomic writes, paths, version) is routed through this one module so the engine
files stay clean and the whole package degrades gracefully on a bare `pip install navig-blackbox`.
When navig IS present, these delegate to the real navig helpers so the two never diverge.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any


def get_console() -> Any:
    """navig's themed console, or a plain rich Console standalone."""
    try:
        from navig.console_helper import get_console as _g

        return _g()
    except Exception:  # noqa: BLE001 - navig absent or console helper unavailable
        from rich.console import Console

        return Console()


def atomic_write_text(path: Path, text: str) -> None:
    """Crash-safe text write (temp file + atomic replace). Delegates to navig's
    hardened writer when present, else a vendored equivalent."""
    try:
        from navig.core.yaml_io import atomic_write_text as _a

        _a(path, text)
        return
    except Exception:  # noqa: BLE001 - navig absent
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(p)


def blackbox_dir() -> Path:
    """The blackbox data dir. navig owns the canonical path; standalone we resolve the SAME
    default (``<data_dir>/blackbox``, honoring NAVIG_DATA_DIR/NAVIG_CONFIG_DIR) so a later
    ``pip install navig`` reads the very same events."""
    try:
        from navig.platform.paths import blackbox_dir as _b

        return _b()
    except Exception:  # noqa: BLE001
        d = _data_dir_fallback() / "blackbox"
        d.mkdir(parents=True, exist_ok=True)
        return d


def _is_system_service_fallback() -> bool:
    """Mirror of navig.platform.paths._is_system_service().

    This branch used to be missing entirely, and its absence is invisible by construction:
    the fallback only runs when navig is ABSENT, so nobody exercising a navig install ever
    sees it. Measured divergence under NAVIG_SYSTEM_SERVICE=1 before the fix —
    core `/etc/navig`, fallback `~/.navig`; core `/var/lib/navig`, fallback `~/.navig/data`.
    A root-run standalone service therefore wrote its blackbox events somewhere navig would
    never read them.
    """
    if os.environ.get("NAVIG_SYSTEM_SERVICE") == "1":
        return True
    if sys.platform == "win32":
        return False
    try:
        import pwd  # noqa: PLC0415

        if pwd.getpwuid(os.getuid()).pw_name == "navig":  # type: ignore[attr-defined]
            return True
    except (ImportError, KeyError, AttributeError, OSError):
        pass
    # INVOCATION_ID is set by systemd for USER-level services too, so uid must also be 0 —
    # otherwise a user-level daemon gets redirected to the system paths.
    try:
        return bool(os.environ.get("INVOCATION_ID")) and os.getuid() == 0  # type: ignore[attr-defined]
    except AttributeError:
        return False


def _config_dir_fallback() -> Path:
    """Mirrors navig.platform.paths.config_dir() branch for branch."""
    env = os.environ.get("NAVIG_CONFIG_DIR")
    if env:
        return Path(env)
    if _is_system_service_fallback():
        return Path("/etc/navig")
    return Path.home() / ".navig"


def _data_dir_fallback() -> Path:
    """Mirrors navig.platform.paths.data_dir() branch for branch.

    Note the system-service target is /var/lib/navig — NOT config_dir()/data. Deriving it
    from config_dir would give /etc/navig/data, which is a second, different wrong answer.
    """
    env = os.environ.get("NAVIG_DATA_DIR")
    if env:
        return Path(env)
    if _is_system_service_fallback():
        return Path("/var/lib/navig")
    return _config_dir_fallback() / "data"


def config_dir() -> Path:
    try:
        from navig.platform.paths import config_dir as _c

        return _c()
    except Exception:  # noqa: BLE001
        return _config_dir_fallback()


def log_dir() -> Path:
    """Mirrors navig.platform.paths.log_dir() — OS-idiomatic (Windows %LOCALAPPDATA%,
    macOS ~/Library/Logs, else $XDG_STATE_HOME) so log tails resolve to navig's real dir."""
    try:
        from navig.platform.paths import log_dir as _l

        return _l()
    except Exception:  # noqa: BLE001
        env = os.environ.get("NAVIG_LOG_DIR")
        if env:
            return Path(env)
        if sys.platform == "win32":
            base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
            return Path(base) / "navig" / "logs"
        if sys.platform == "darwin":
            return Path.home() / "Library" / "Logs" / "navig"
        xdg = os.environ.get("XDG_STATE_HOME")
        return (Path(xdg) if xdg else Path.home() / ".local" / "state") / "navig" / "logs"


def debug_log_path() -> Path:
    try:
        from navig.platform.paths import debug_log_path as _d

        return _d()
    except Exception:  # noqa: BLE001
        return log_dir() / "debug.log"


def navig_version() -> str:
    try:
        from navig import __version__  # type: ignore[attr-defined]

        return str(__version__)
    except Exception:  # noqa: BLE001
        return "standalone"
