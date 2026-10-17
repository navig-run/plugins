"""navig-cabinet plugin entry point.

  * **CLI** — ``cabinet`` registers via the ``navig.commands`` entry-point group.
  * **Module catalog** — registers a FREE, toggleable ``cabinet`` ModuleDef so the
    deck/os surfaces can show it.

  * **Deck routes** — ``/api/deck/cabinet/*`` for the desktop Cabinet app, mounted on
    the ``gateway:register_routes`` hook. They answer ONLY this computer and return
    metadata only; see :mod:`navig_cabinet.deck_routes` for why.

Expiry reminders are ticked by core's notify scheduler (soft import), not a worker here.
"""

from __future__ import annotations

import logging

from navig_sdk.host import command_name  # noqa: E402

# The command a user types here: `navig cabinet` inside navig, `navig-cabinet` on its own.
CMD = command_name("cabinet", standalone="navig-cabinet")

_log = logging.getLogger(__name__)
_REGISTERED = False


def _cabinet_module_def():
    from navig.modules.registry import ModuleDef, ModuleKind

    return ModuleDef(
        id="cabinet",
        label="Cabinet",
        description=(
            "An encrypted cabinet for your important files — ID and passport scans, medical "
            "records, contracts, photos, recordings. Searchable by the text inside them, "
            "with expiry reminders and portable backups."
        ),
        kind=ModuleKind.APP,
        category="tools",  # operate | grow | build | tools | system (registry.py)
        icon="archive",
        capability=None,  # free — toggled via the module registry, not a license
        surfaces=["os-tile:cabinet", "cli:cabinet"],
        requires=["gateway"],
        app_category="Security",
        about=[
            "Your important files, encrypted on this computer: ID and passport scans, "
            "medical records, insurance, contracts, photos and recordings.",
            "Search by the words inside a scan (read locally, never sent anywhere), and "
            "get reminded before a passport or policy expires.",
        ],
        features=[
            "Search titles, tags and the text inside documents",
            "Expiry reminders 90 / 30 / 7 days before, and once expired",
            "Open in the usual app — a short-lived decrypted copy on this computer only",
            f"Portable encrypted backups ({CMD} backup)",
        ],
        default_enabled=True,
        source="plugin",
    )


def register() -> None:
    """Register the free ``cabinet`` module (idempotent)."""
    global _REGISTERED
    if _REGISTERED:
        return
    try:
        from navig.modules.registry import register_module

        register_module(_cabinet_module_def())
    except Exception as exc:  # noqa: BLE001 — never break boot on a catalog hiccup
        _log.debug("cabinet module registration skipped: %s", exc)
    try:
        from navig.core.hooks import register_hook
    except Exception as exc:  # noqa: BLE001 — core too old / partial install
        _log.debug("cabinet: hook bus unavailable (%s)", exc)
        return
    register_hook("gateway:register_routes", _register_deck_routes)
    _REGISTERED = True


def _register_deck_routes(event) -> None:
    """Handler for ``gateway:register_routes`` — mounts the Cabinet deck routes."""
    app = (getattr(event, "context", None) or {}).get("app")
    if app is None:
        return
    from navig_cabinet import deck_routes

    deck_routes.register(app)
