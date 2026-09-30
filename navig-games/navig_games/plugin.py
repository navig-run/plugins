"""navig-games plugin entry point.

  * **CLI** — ``games`` registers via the ``navig.commands`` entry-point group.
  * **Module catalog** — registers a FREE, toggleable ``games`` ModuleDef so the
    Store/deck/os surfaces show it.
  * **Agent tools** — ``games_check`` / ``games_library`` / ``games_deals`` /
    ``games_claim`` on the agent registry.
  * **Deck API** — ``/api/deck/games/*`` mounts via core's ``gateway:register_routes``
    hook (gated on the ``games`` module), so the deck / OS / remote can consume the
    engine over HTTP. Mirrors navig-social.

Everything is best-effort and degraded-never-blocks: a failure here must never
stop ``navig`` from booting.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)
_REGISTERED = False


def _games_module_def():
    from navig.modules.registry import ModuleDef, ModuleKind

    return ModuleDef(
        id="games",
        label="Games",
        description="Free games, wishlist deals & your game library across launchers.",
        kind=ModuleKind.APP,
        category="grow",
        icon="gamepad-2",
        capability=None,  # free
        # os-tile:games surfaces it as a desktop app in navig-os (Life category);
        # cli:games is the terminal surface. The HTTP API is /api/deck/games/*.
        surfaces=["cli:games", "os-tile:games"],
        requires=["gateway"],
        default_enabled=True,
        source="plugin",
        app_category="Life",
        scope="brain",
    )


def register() -> None:
    """Idempotently register the ``games`` module + agent tools. Called by core's
    ``load_entry_point_plugins()``."""
    global _REGISTERED
    if _REGISTERED:
        return
    try:
        from navig.modules.registry import register_module

        register_module(_games_module_def())
    except Exception as exc:  # noqa: BLE001
        _log.warning("navig-games: module registry unavailable (%s)", exc)

    try:
        from navig_games.agent_tools import register_games_tools

        register_games_tools()
    except Exception as exc:  # noqa: BLE001
        _log.warning("navig-games: agent tools not registered (%s)", exc)

    try:
        from navig.core.hooks import register_hook

        register_hook("gateway:register_routes", _register_deck_routes)
    except Exception as exc:  # noqa: BLE001 — hook bus unavailable / core too old
        _log.warning("navig-games: gateway route hook not registered (%s)", exc)

    _REGISTERED = True


def _register_deck_routes(event) -> None:
    """Handler for ``gateway:register_routes`` — mounts the games API on the gateway
    app (gated on the ``games`` module being enabled)."""
    app = (getattr(event, "context", None) or {}).get("app")
    if app is None:
        return
    try:
        from navig_games.deck_routes import games as games_routes

        games_routes.register(app)
        _log.debug("navig-games: deck API routes mounted")
    except Exception as exc:  # noqa: BLE001
        _log.warning("navig-games: could not mount deck routes (%s)", exc)
