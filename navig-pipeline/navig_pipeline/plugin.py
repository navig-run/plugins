"""navig-pipeline plugin entry point.

Registers the free ``pipeline`` module. The ``navig pipeline`` CLI (run / status)
is discovered via the ``navig.commands`` entry-point group. The orchestrator
itself imports nothing at boot — it soft-imports each capability only when a
stage runs, so it degrades gracefully to whatever plugins are installed.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)
_REGISTERED = False


def _pipeline_module_def():
    from navig.modules.registry import ModuleDef, ModuleKind

    return ModuleDef(
        id="pipeline",
        label="Pipeline",
        description="The content assembly line — download → transcribe → script → narrate → fan-out.",
        kind=ModuleKind.APP,
        category="grow",
        icon="workflow",
        capability=None,  # free — composition over the media plugin family
        surfaces=["cli:pipeline"],
        default_enabled=True,
        source="plugin",
    )


def register() -> None:
    """Idempotently register the ``pipeline`` module. Called at gateway boot."""
    global _REGISTERED
    if _REGISTERED:
        return
    try:
        from navig.modules.registry import register_module

        register_module(_pipeline_module_def())
    except Exception as exc:  # pragma: no cover
        _log.warning("navig-pipeline: module registry unavailable (%s)", exc)
    _REGISTERED = True
    _log.info("navig-pipeline: pipeline module registered")
