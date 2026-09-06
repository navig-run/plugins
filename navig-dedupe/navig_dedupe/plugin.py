"""navig-dedupe plugin entry point.

Mirrors the minimal exemplar (navig-github): CLI verb via the ``navig.commands``
entry-point group, and a FREE, toggleable ``dedupe`` ModuleDef so the Store / deck
/ os surfaces show it. No gateway routes.

Kept deliberately light — this module is imported at navig gateway boot, so it must
NOT pull the numpy/Pillow engines (those load lazily only when a scan runs).
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)
_REGISTERED = False


def _dedupe_module_def():
    from navig.modules.registry import ModuleDef, ModuleKind

    return ModuleDef(
        id="dedupe",
        label="Dedupe",
        description="Find & quarantine duplicate photos, video, audio & files (non-destructive).",
        kind=ModuleKind.APP,
        category="tools",
        icon="copy",
        capability=None,  # free
        surfaces=["cli:dedupe"],
        default_enabled=True,
        source="plugin",
        app_category="Systems",
    )


def register() -> None:
    """Idempotently register the ``dedupe`` module. Called by core's
    ``load_entry_point_plugins()``."""
    global _REGISTERED
    if _REGISTERED:
        return
    try:
        from navig.modules.registry import register_module

        register_module(_dedupe_module_def())
    except Exception as exc:  # pragma: no cover
        _log.warning("navig-dedupe: module registry unavailable (%s)", exc)
    _REGISTERED = True
