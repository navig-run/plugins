"""navig-blackbox plugin entry point.

Registers a FREE, toggleable ``blackbox`` module so the Store / deck / os surfaces show it.
The ``navig blackbox`` CLI verb stays a core command (backed by this engine via the shim), so
this module deliberately does NOT register a ``navig.commands`` entry — it only surfaces the
capability in the catalog.

Kept light — imported at navig gateway boot; it must not pull the engine.
"""
from __future__ import annotations

import logging

_log = logging.getLogger(__name__)
_REGISTERED = False


def _blackbox_module_def():
    from navig.modules.registry import ModuleDef, ModuleKind

    return ModuleDef(
        id="blackbox",
        label="Blackbox",
        description="Flight-recorder & crash black-box: event log + sealed .navbox incident bundles.",
        kind=ModuleKind.APP,
        category="system",
        icon="box",
        capability=None,  # free
        surfaces=["cli:blackbox"],
        default_enabled=True,
        source="plugin",
        app_category="Systems",
    )


def register() -> None:
    """Idempotently register the ``blackbox`` module. Called by core's
    ``load_entry_point_plugins()``."""
    global _REGISTERED
    if _REGISTERED:
        return
    try:
        from navig.modules.registry import register_module

        register_module(_blackbox_module_def())
    except Exception as exc:  # pragma: no cover
        _log.warning("navig-blackbox: module registry unavailable (%s)", exc)
    _REGISTERED = True
