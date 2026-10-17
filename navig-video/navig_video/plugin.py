"""navig-video plugin entry point.

Registers the free ``video`` module so it shows in the Store/deck. The CLI
(``navig video``) is discovered via the ``navig.commands`` entry-point group —
no core edit needed (``video`` is free in ``_EXTERNAL_CMD_MAP``).
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)
_REGISTERED = False


def _video_module_def():
    from navig.modules.registry import ModuleDef, ModuleKind

    return ModuleDef(
        id="video",
        label="Video",
        description="Render a channel's video formats — kinetic episodes, bulletins, variants.",
        kind=ModuleKind.APP,
        category="grow",
        icon="clapperboard",
        capability=None,  # free — toggled via the module registry, not a license
        surfaces=["cli:video"],
        default_enabled=True,
        source="plugin",
    )


def register() -> None:
    """Idempotently register the ``video`` module. Called at gateway boot."""
    global _REGISTERED
    if _REGISTERED:
        return
    try:
        from navig.modules.registry import register_module

        register_module(_video_module_def())
    except Exception as exc:  # pragma: no cover - registry is optional at boot
        _log.warning("navig-video: module registry unavailable (%s)", exc)
    _REGISTERED = True
    _log.info("navig-video: video module registered")
