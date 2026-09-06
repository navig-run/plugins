"""navig-explore plugin entry point.

Wires the universal media/data **explorer** into a running NAVIG daemon without
any of it living in public core (mirrors ``navig-download``):

  * **CLI** — ``explore`` registers via the ``navig.commands`` entry-point group
    (pyproject.toml), discovered by ``navig.cli.registration``.
  * **Module catalog** — registers a FREE, toggleable ``explore`` ModuleDef so the
    deck/os surfaces show it.

Pure CLI (no gateway/deck routes) — the explorer server (``navig_explore.explorer``)
is bundled and runs in-process, serving a local web UI for any folder.
"""
from __future__ import annotations

import logging

_log = logging.getLogger(__name__)
_REGISTERED = False


def _explore_module_def():
    from navig.modules.registry import ModuleDef, ModuleKind

    return ModuleDef(
        id="explore",
        label="Explore",
        description=("Universal local media/data explorer for any folder — filter, preview, "
                     "organize. Photo libraries also get offline recognition: search by "
                     "person, place, object or scene, and recover capture dates EXIF lost."),
        kind=ModuleKind.APP,
        category="grow",
        icon="folder-search",
        capability=None,  # free — toggled via the module registry, not a license
        surfaces=["cli:explore"],
        default_enabled=True,
        source="plugin",
    )


def register() -> None:
    """Register the free ``explore`` module (idempotent)."""
    global _REGISTERED
    if _REGISTERED:
        return
    try:
        from navig.modules.registry import register_module
        register_module(_explore_module_def())
        _REGISTERED = True
    except Exception as exc:  # noqa: BLE001 — never break boot on a catalog hiccup
        _log.debug("explore module registration skipped: %s", exc)
