"""navig-generate plugin entry point.

Wires the AI media **generation** deck surface into a running NAVIG daemon
(mirrors ``navig-harbor``). The generation *engine* lives in core
(``navig.media.generation_service``); this plugin only mounts its deck routes.

  * **Deck routes** — the generation routes (`/api/deck/media/*`) register via
    core's ``gateway:register_routes`` hook at gateway boot.
  * **Module catalog** — registers a FREE, toggleable ``generate`` ModuleDef;
    switching it off makes the routes return ``403 module_disabled`` (they're
    wrapped in ``requires_module("generate")``).

The `navig generate` CLI (analyse + generate) is a core built-in — this plugin
does NOT register a CLI (`navig media` remains as a deprecated alias of it).
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)
_REGISTERED = False


def _generate_module_def():
    from navig.modules.registry import ModuleDef, ModuleKind

    return ModuleDef(
        id="generate",
        label="Generate",
        description="AI media generation — images, video & audio.",
        kind=ModuleKind.APP,
        category="grow",
        icon="sparkles",
        capability=None,  # free — toggled via the module registry, not a license
        # Desktop tile (deck→OS migration 2026-07-12; the deck's media app was
        # deleted — the OS Generate app owns this surface now).
        surfaces=["os-tile:generate"],
        requires=["gateway"],
        default_enabled=True,
        source="plugin",
        app_category="Create",
        about=[
            "Compose a brief — prompt, placement, an optional reference screenshot — "
            "and Generate creates on-brand images, video and audio with your project's "
            "palette and house style auto-injected.",
            "Flip through variants, keep the good ones into the active space's refs "
            "library, and derive: cutout, sprite-ify, instruction-edit or redesign.",
        ],
        features=[
            "Image, video & audio modalities with per-provider keys",
            "Project palette + style note auto-injected into every prompt",
            "Variant flow: Keep · Next · Reject, with full history",
            "Derivative ops: background cutout, game-sprite pipeline, edit, redesign",
        ],
    )


def register() -> None:
    """Idempotently register the ``generate`` module + the gateway route hook.

    Called by core's ``load_entry_point_plugins()`` at gateway boot.
    """
    global _REGISTERED
    if _REGISTERED:
        return

    try:
        from navig.modules.registry import register_module

        register_module(_generate_module_def())
    except Exception as exc:  # pragma: no cover
        _log.warning("navig-generate: module registry unavailable (%s)", exc)

    try:
        from navig.core.hooks import register_hook
    except Exception as exc:  # pragma: no cover — core too old / partial install
        _log.warning("navig-generate: hook bus unavailable (%s)", exc)
        return

    register_hook("gateway:register_routes", _register_deck_routes)
    _REGISTERED = True
    _log.info("navig-generate: media generation route hook + generate module registered")


def _register_deck_routes(event) -> None:
    """Handler for ``gateway:register_routes`` — mounts the AI generation deck routes
    (each gated on the ``generate`` module being enabled)."""
    app = (getattr(event, "context", None) or {}).get("app")
    if app is None:
        return
    from navig_generate.deck_routes import generation

    generation.register(app)
    _log.debug("navig-generate: media generation deck routes mounted")
