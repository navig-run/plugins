"""navig-antivirus plugin entry point.

Mirrors the other first-party plugins (navig-github/navig-explore):

  * **CLI** — ``antivirus`` registers via the ``navig.commands`` entry-point
    group (pyproject.toml) → ``navig antivirus <cmd>``.
  * **Module catalog** — registers a FREE, toggleable ``antivirus`` ModuleDef so
    the Store/deck/os surfaces show it with a health badge.

All capabilities are FREE (no license capability).
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)
_REGISTERED = False


def _antivirus_module_def():
    from navig.modules.registry import ModuleDef, ModuleKind

    return ModuleDef(
        id="antivirus",
        label="Antivirus",
        description="Browser-extension malware/PUP scan, Chrome registry audit, "
        "profile recovery & system malware scan (Defender).",
        kind=ModuleKind.LAUNCHER,  # a standalone tool NAVIG launches (not a deck app)
        category="diagnostics",
        icon="shield",
        capability=None,  # free
        surfaces=["cli:antivirus"],
        default_enabled=True,
        source="plugin",
    )


def register() -> None:
    """Idempotently register the ``antivirus`` module. Called by core's
    ``load_entry_point_plugins()`` at gateway boot. Failure here never blocks
    boot or the CLI (the ``navig antivirus`` verb mounts independently)."""
    global _REGISTERED
    if _REGISTERED:
        return
    try:
        from navig.modules.registry import register_module

        register_module(_antivirus_module_def())
    except Exception as exc:  # pragma: no cover — best-effort, never blocks boot
        _log.warning("navig-antivirus: module registry unavailable (%s)", exc)
    _REGISTERED = True
