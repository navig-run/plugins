"""The few navig services the browser engine uses, with a standalone twin when navig is absent.

Each helper resolves navig's own implementation AT CALL TIME, so inside navig nothing changes
(and a test that patches ``navig.config.get_config_manager`` still reaches the engine); without
navig it answers from the same files through navig-sdk.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any


def get_logger(subsystem: str) -> Any:
    """navig's structured logger ``navig.<subsystem>`` when installed, else a plain one of that name.

    The name stays ``navig.browser.*`` either way, so inside navig the engine keeps writing to
    navig's debug log exactly as it did before it moved.
    """
    try:
        from navig.core.logging import get_logger as _impl  # noqa: PLC0415
    except ImportError:
        return logging.getLogger(f"navig.{subsystem}")
    return _impl(subsystem)


class _StandaloneConfig:
    """The slice of navig's ConfigManager the engine reads: ``global_config`` and ``get``."""

    @property
    def global_config(self) -> dict[str, Any]:
        from navig_sdk import files  # noqa: PLC0415
        from navig_sdk.host import config_dir  # noqa: PLC0415

        data = files.safe_load_yaml(config_dir() / "config.yaml")
        return data if isinstance(data, dict) else {}

    def get(self, dotted: str, default: Any = None) -> Any:
        from navig_sdk.settings import get_path  # noqa: PLC0415

        return get_path(dotted, default)


def get_config_manager() -> Any:
    """navig's ConfigManager inside navig; standalone, the same config.yaml read through navig-sdk."""
    try:
        from navig.config import get_config_manager as _impl  # noqa: PLC0415
    except ImportError:
        return _StandaloneConfig()
    return _impl()


def resolve_user_path(given: Path | str) -> Path:
    """A path the operator TYPED, resolved against the directory they typed it in.

    navig chdir's into the active space before a command runs, so it records where the operator
    stood in ``NAVIG_INVOCATION_CWD``; navig's ``resolve_user_path`` reads it. On its own the
    process cwd is already where the operator stands.
    """
    try:
        from navig.platform.paths import resolve_user_path as _impl  # noqa: PLC0415
    except ImportError:
        p = Path(given).expanduser()
        if p.is_absolute():
            return p.resolve()
        origin = os.environ.get("NAVIG_INVOCATION_CWD")
        base = Path(origin) if origin and Path(origin).is_dir() else Path.cwd()
        return (base / p).resolve()
    return _impl(given)


_bg_tasks: set[Any] = set()


def spawn(coro: Any, *, name: str | None = None) -> Any:
    """navig's GC-safe fire-and-forget (``navig.core.background.spawn``), or the same without navig.

    A bare ``asyncio.ensure_future`` is only weakly referenced by the loop, so a task nobody
    holds can be collected mid-run; this keeps a strong reference until it finishes and logs
    what it raised instead of letting it vanish.
    """
    try:
        from navig.core.background import spawn as _impl  # noqa: PLC0415
    except ImportError:
        import asyncio  # noqa: PLC0415

        task = asyncio.ensure_future(coro)
        if name:
            task.set_name(name)
        _bg_tasks.add(task)

        def _done(t: Any) -> None:
            _bg_tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                logging.getLogger("navig.browser").warning(
                    "background task %r failed: %r", t.get_name(), t.exception()
                )

        task.add_done_callback(_done)
        return task
    return _impl(coro, name=name)
