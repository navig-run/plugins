"""navig-text plugin entry point.

Registers the free ``text`` module and wires the TEXT generation facet into
core's shared engine (:func:`navig_text.generation.register_text_facet`), so
``navig generate gen --modality text`` dispatches through this plugin. The
``navig text`` CLI (gen / check) is discovered via the ``navig.commands``
entry-point group. Text generation uses core's AI client — no external deps.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)
_REGISTERED = False


def _text_module_def():
    from navig.modules.registry import ModuleDef, ModuleKind

    return ModuleDef(
        id="text",
        label="Text",
        description="AI text generation — drafts, articles, captions, and fan-out briefs.",
        kind=ModuleKind.APP,
        category="grow",
        icon="pen-tool",
        capability=None,  # free — toggled via the module registry, not a license
        surfaces=["cli:text"],
        default_enabled=True,
        source="plugin",
    )


def register() -> None:
    """Idempotently register the ``text`` module + TEXT facet. Called at gateway boot."""
    global _REGISTERED
    if _REGISTERED:
        return
    try:
        from navig.modules.registry import register_module

        register_module(_text_module_def())
    except Exception as exc:  # pragma: no cover
        _log.warning("navig-text: module registry unavailable (%s)", exc)
    # Wire the TEXT generation facet into core's shared engine (idempotent + defensive).
    try:
        from navig_text.generation import register_text_facet

        register_text_facet()
    except Exception as exc:  # pragma: no cover
        _log.debug("navig-text: text facet registration skipped (%s)", exc)
    _REGISTERED = True
    _log.info("navig-text: text (generation) module registered")
