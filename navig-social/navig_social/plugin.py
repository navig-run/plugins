"""navig-social plugin entry point.

Wires the **Social** app — compose / schedule / fan-out publish — into a running
NAVIG daemon without any of it living in public core (mirrors ``navig-harbor``):

  * **CLI** — ``social`` / ``facebook`` (``fb``) register via the
    ``navig.commands`` entry-point group (pyproject.toml), discovered by
    ``navig.cli.registration``.
  * **Deck routes** — the Social composer/scheduler routes register via core's
    ``gateway:register_routes`` hook, fired after ``load_entry_point_plugins()``
    imports this module at gateway boot. (The wire endpoint keeps its stable
    internal name ``/api/deck/studio/*`` — the app it serves is "Social".)
  * **Module catalog** — registers the FREE, toggleable ``social`` ModuleDef that
    is the desktop app tile (``os-tile:social``). ONE module owns the whole
    surface — tile, routes (``requires_module("social")``) and scheduler — so
    switching "Social" off disables all of it together and there is no separate
    "Studio" module to drift out of sync.

Social is FREE (no license capability) — enable/disable is purely the module
registry override (``modules.overrides``).
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)
_REGISTERED = False


def _social_module_def():
    from navig.modules.registry import ModuleDef, ModuleKind

    return ModuleDef(
        id="social",
        label="Social",
        description="Compose, schedule & publish across your social networks.",
        kind=ModuleKind.APP,
        category="grow",
        icon="sparkles",
        capability=None,  # free — toggled via the module registry, not a license
        # THE Social app tile (deck→OS migration): one module owns the whole
        # surface — the OS tile, the deck routes (`requires_module("social")`),
        # and the scheduler — so toggling "Social" governs all of it, with no
        # second "Studio" module to fall out of sync. `cli:*` keep the standalone
        # `navig social` / `navig facebook` commands.
        surfaces=["os-tile:social", "cli:social", "cli:facebook"],
        requires=["gateway"],
        default_enabled=True,
        source="plugin",
        app_category="Create",
        about=[
            "Your content desk: write a post once — markdown, media, AI assist — and "
            "Social schedules it or fans it out across every network you've connected, "
            "from a single draft.",
            "Compose, queue, a publishing calendar, and a full history of what went "
            "where. Messaging channels (Telegram, Discord, WhatsApp) double as publish "
            "targets alongside the social publishers.",
        ],
        features=[
            "Compose with markdown, media attachments & AI assist (draft / rewrite / hashtags)",
            "Schedule once or recurring, or publish now — with a calendar & queue",
            "Fan-out across connected networks + messaging channels from one draft",
            "Publish history and per-network connection status",
        ],
    )


def register() -> None:
    """Idempotently register the ``social`` module + the gateway route hook.

    Called by core's ``load_entry_point_plugins()`` at gateway boot.
    """
    global _REGISTERED
    if _REGISTERED:
        return

    # Catalog entry (best-effort — a missing/old registry never blocks load).
    try:
        from navig.modules.registry import register_module

        register_module(_social_module_def())
    except Exception as exc:  # pragma: no cover
        _log.warning("navig-social: module registry unavailable (%s)", exc)

    # Deck route hook.
    try:
        from navig.core.hooks import register_hook
    except Exception as exc:  # pragma: no cover — core too old / partial install
        _log.warning("navig-social: hook bus unavailable (%s)", exc)
        return

    register_hook("gateway:register_routes", _register_deck_routes)
    _REGISTERED = True
    _log.info("navig-social: Studio route hook + social module registered")


def _register_deck_routes(event) -> None:
    """Handler for ``gateway:register_routes`` — mounts the Studio scheduler deck
    routes on the gateway app (each gated on the ``social`` module being enabled)."""
    app = (getattr(event, "context", None) or {}).get("app")
    if app is None:
        return
    from navig_social.deck_routes import engagement, redirect, studio

    studio.register(app)
    engagement.register(app)
    redirect.register(app)
    _log.debug("navig-social: Studio + engagement + redirect routes mounted")
