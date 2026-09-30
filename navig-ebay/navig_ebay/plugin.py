"""navig-ebay plugin entry point.

Mirrors the other first-party plugins:

  * **CLI** — ``ebay`` registers via the ``navig.commands`` entry-point group
    (pyproject.toml), so ``navig ebay`` mounts as a top-level verb.
  * **Module catalog** — registers a FREE, toggleable ``ebay`` ModuleDef so the
    Store/deck/os surfaces show it.

eBay selling tools are FREE (no license capability). Token resolution stays the
core pattern: vault (provider ``ebay``, profile ``oauth``) → EBAY_ACCESS_TOKEN
env → config.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)
_REGISTERED = False


def _ebay_module_def():
    from navig.modules.registry import ModuleDef, ModuleKind

    return ModuleDef(
        id="ebay",
        label="eBay",
        description="Sell on eBay: draft, publish, price & manage listings via the official Sell APIs.",
        kind=ModuleKind.APP,
        category="build",
        icon="ebay",
        capability=None,  # free
        surfaces=["cli:ebay"],
        default_enabled=True,
        source="plugin",
    )


def register() -> None:
    """Idempotently register the ``ebay`` module. Called by core's
    ``load_entry_point_plugins()``."""
    global _REGISTERED
    if _REGISTERED:
        return
    try:
        from navig.modules.registry import register_module

        register_module(_ebay_module_def())
    except Exception as exc:  # pragma: no cover — registry unavailable is non-fatal
        _log.warning("navig-ebay: module registry unavailable (%s)", exc)
    _REGISTERED = True
