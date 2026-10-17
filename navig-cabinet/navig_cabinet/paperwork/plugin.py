"""`navig paperwork` entry point — part of navig-cabinet.

Wires the paperwork filer into a running NAVIG daemon without any of it living in
core (mirrors ``navig-explore``):

  * **CLI** — ``paperwork`` registers via the ``navig.commands`` entry-point group
    (pyproject.toml), discovered by ``navig.cli.registration``.
  * **Module catalog** — registers a FREE, toggleable ``paperwork`` ModuleDef so the
    deck/os surfaces show it.

Pure CLI: no gateway routes, no background workers.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)
_REGISTERED = False


def _paperwork_module_def():
    from navig.modules.registry import ModuleDef, ModuleKind

    return ModuleDef(
        id="paperwork",
        label="Paperwork",
        description=(
            "File business paperwork into a space — invoices, quotes, contracts, tax. "
            "Reads the documents themselves, so issued and received invoices separate "
            "correctly; deduped, hash-verified and reversible."
        ),
        kind=ModuleKind.APP,
        category="tools",  # operate | grow | build | tools | system (registry.py)
        icon="file-text",
        capability=None,  # free — toggled via the module registry, not a license
        surfaces=["cli:paperwork"],
        default_enabled=True,
        source="plugin",
    )


def register() -> None:
    """Register the free ``paperwork`` module (idempotent)."""
    global _REGISTERED
    if _REGISTERED:
        return
    try:
        from navig.modules.registry import register_module

        register_module(_paperwork_module_def())
        _REGISTERED = True
    except Exception as exc:  # noqa: BLE001 — never break boot on a catalog hiccup
        _log.debug("paperwork module registration skipped: %s", exc)
