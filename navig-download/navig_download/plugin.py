"""navig-download plugin entry point.

Wires the universal media **downloader** into a running NAVIG daemon without any
of it living in public core (mirrors ``navig-github``):

  * **CLI** — ``download`` / ``dl`` (and back-compat ``tiktok`` / ``tt``) register via
    the ``navig.commands`` entry-point group (pyproject.toml), discovered by
    ``navig.cli.registration``.
  * **Module catalog** — registers a FREE, toggleable ``download`` ModuleDef so the
    deck/os surfaces show it.

This plugin is pure CLI (no gateway/deck routes) — the yt-dlp engine
(``navig_download.downloader``) is bundled and runs in-process.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)
_REGISTERED = False


def _download_module_def():
    from navig.modules.registry import ModuleDef, ModuleKind

    return ModuleDef(
        id="download",
        label="Download",
        description="Universal media downloader (yt-dlp) — video, audio & files from any site.",
        kind=ModuleKind.APP,
        category="grow",
        icon="download",
        capability=None,  # free — toggled via the module registry, not a license
        surfaces=["cli:download", "cli:tiktok"],
        default_enabled=True,
        source="plugin",
    )


def register() -> None:
    """Idempotently register the ``download`` module.

    Called by core's ``load_entry_point_plugins()`` at gateway boot.
    """
    global _REGISTERED
    if _REGISTERED:
        return
    try:
        from navig.modules.registry import register_module

        register_module(_download_module_def())
    except Exception as exc:  # pragma: no cover
        _log.warning("navig-download: module registry unavailable (%s)", exc)
    _REGISTERED = True
    _log.info("navig-download: download module registered")
