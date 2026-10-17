"""navig-devhost plugin entry point.

Mirrors the minimal exemplar shape (navig-github): the CLI mounts via the
``navig.commands`` entry-point group; this registers a FREE, toggleable
``devhost`` ModuleDef so Store / deck / menu surfaces list it. No gateway routes.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)
_REGISTERED = False


def _devhost_module_def():
    from navig.modules.registry import ModuleDef, ModuleKind

    return ModuleDef(
        id="devhost",
        label="Dev Host",
        description="Local .test domains with trusted HTTPS for any dev server (hosts + mkcert + relay).",
        kind=ModuleKind.APP,
        category="build",
        icon="globe",
        capability=None,  # free
        surfaces=["cli:devhost"],
        default_enabled=True,
        source="plugin",
    )


def register() -> None:
    """Idempotently register the ``devhost`` module. Called by core's
    ``load_entry_point_plugins()``."""
    global _REGISTERED
    if _REGISTERED:
        return
    try:
        from navig.modules.registry import register_module

        register_module(_devhost_module_def())
    except Exception as exc:  # pragma: no cover
        _log.warning("navig-devhost: module registry unavailable (%s)", exc)
    _REGISTERED = True
