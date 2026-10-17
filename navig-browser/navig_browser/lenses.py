"""Lenses in navig-browser — the user's Tampermonkey-compatible scripts on every context.

The store, the parser and the page runtime live in navig core (``navig.lenses``; spec
``docs/lenses.md``). This module is the ONE place the browser engine touches them: every
controller calls :func:`apply_lenses` right after it creates or attaches a context, so a
Lens behaves the same in ``navig cdp``, the agent's browser tools and the MCP tools.

Best-effort by design. Lenses are an augmentation, never a dependency: navig-browser runs
standalone (no navig core), against an older core that predates Lenses, and offline — in
every one of those cases this registers nothing and the browser works exactly as before.

What it covers, honestly: ``add_init_script`` registers the Lenses on THIS connection. A
browser NAVIG launched and you then use by hand keeps them only while some navig process
is attached — always-on Lenses in your own Chrome are the extension's job.

Opt out per process with ``NAVIG_LENSES=0`` (or ``off``/``false``/``no``).
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from navig_browser._compat import get_logger

logger = get_logger("browser.lenses")

_OFF = {"0", "false", "off", "no"}


def lenses_enabled() -> bool:
    return os.environ.get("NAVIG_LENSES", "").strip().lower() not in _OFF


def _compiled_codes(host: str) -> list[str]:
    try:
        from navig.lenses import LensStore  # noqa: PLC0415 — optional: core may be absent/older
    except ImportError:
        return []
    return [lens.code for lens in LensStore().compiled(host).lenses]


async def apply_lenses(context: Any, *, host: str = "playwright") -> int:
    """Register every enabled, entitled Lens on *context*. Returns how many were added.

    Never raises: a broken store, a missing core or a context that refuses an init script
    costs the Lenses, not the browser session.
    """
    if context is None or not lenses_enabled():
        return 0
    try:
        codes = await asyncio.to_thread(_compiled_codes, host)
    except Exception as exc:  # noqa: BLE001 — never fail a browser start over a Lens
        logger.warning("[lenses] could not load Lenses: %s", exc)
        return 0
    added = 0
    for code in codes:
        try:
            await context.add_init_script(code)
            added += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("[lenses] init script refused: %s", exc)
    if added:
        logger.info("[lenses] %d Lens(es) active on this browser context", added)
    return added


def compile_userscript(source: str, host: str = "playwright") -> str | None:
    """Compile a ``.user.js`` (one with a ``==UserScript==`` header) with the Lens runtime,
    so its ``@match`` / ``@run-at`` / ``@grant`` are honoured. ``None`` when *source* has no
    header or navig core (with Lenses) isn't installed — the caller injects it as-is.

    Raises ``ValueError`` (the header names what to fix) when the header is invalid.
    """
    if "==UserScript==" not in source:
        return None
    try:
        from navig.lenses import (  # noqa: PLC0415
            LensFetchError,
            LensFormatError,
            compile_lens,
            fetch_text,
            parse_manifest,
        )
    except ImportError:
        return None
    try:
        manifest = parse_manifest(source)
        requires = [fetch_text(url) for url in manifest.requires]
    except (LensFormatError, LensFetchError) as exc:
        raise ValueError(str(exc)) from exc
    return compile_lens(manifest, source, host=host, requires=requires).code
