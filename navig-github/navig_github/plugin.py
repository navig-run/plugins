"""navig-github plugin entry point.

The minimal exemplar of an extracted command plugin (mirrors the other first-party plugins, but
with no gateway routes):

  * **CLI** — ``github`` registers via the ``navig.commands`` entry-point
    group (pyproject.toml).
  * **Module catalog** — registers a FREE, toggleable ``github`` ModuleDef so
    the Store/deck/os surfaces show it.

GitHub tools are FREE (no license capability). Token resolution stays the core
pattern: vault (`github_token`) → GITHUB_TOKEN env → config `github.token`.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)
_REGISTERED = False


def _github_module_def():
    from navig.modules.registry import ModuleDef, ModuleKind

    return ModuleDef(
        id="github",
        label="GitHub",
        description="GitHub search, backup, clone & mirror.",
        kind=ModuleKind.APP,
        category="build",
        icon="github",
        capability=None,  # free
        surfaces=["cli:github"],
        default_enabled=True,
        source="plugin",
    )


def register() -> None:
    """Idempotently register the ``github`` module. Called by core's
    ``load_entry_point_plugins()``."""
    global _REGISTERED
    if _REGISTERED:
        return
    try:
        from navig.modules.registry import register_module

        register_module(_github_module_def())
    except Exception as exc:  # pragma: no cover
        _log.warning("navig-github: module registry unavailable (%s)", exc)
    _REGISTERED = True
