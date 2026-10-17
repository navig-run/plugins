"""navig-vm plugin entry point.

The CLI mounts through the ``navig.commands`` entry point; this registers a FREE,
toggleable ``vm`` module so the Store / deck / menu surfaces list it. No gateway routes.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)
_REGISTERED = False


def _vm_module_def():
    from navig.modules.registry import ModuleDef, ModuleKind

    return ModuleDef(
        id="vm",
        label="Virtual Machines",
        description="Local VMware VMs from verified cloud images, provisioned on first boot.",
        kind=ModuleKind.APP,
        category="build",
        icon="server",
        capability=None,  # free
        surfaces=["cli:vm"],
        default_enabled=True,
        source="plugin",
    )


def register() -> None:
    """Idempotently register the ``vm`` module. Called by core's ``load_entry_point_plugins()``."""
    global _REGISTERED
    if _REGISTERED:
        return
    try:
        from navig.modules.registry import register_module

        register_module(_vm_module_def())
    except Exception as exc:  # pragma: no cover
        _log.warning("navig-vm: module registry unavailable (%s)", exc)
    _REGISTERED = True
