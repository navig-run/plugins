"""navig-notes plugin entry point.

* **CLI** — ``notes`` registers via the ``navig.commands`` entry-point group.
* **Module catalog** — a FREE, toggleable ``notes`` ModuleDef, so the routes have a gate.
* **Deck routes** — ``/api/deck/notes/*`` for the Dock's sync and NAVIG's own surfaces,
  mounted on the ``gateway:register_routes`` hook. They answer ONLY this computer; see
  :mod:`navig_notes.deck_routes`.
"""

from __future__ import annotations

import logging

from navig_sdk.host import command_name

CMD = command_name("notes", standalone="navig-notes")

_log = logging.getLogger(__name__)
_REGISTERED = False


def _notes_module_def():
    from navig.modules.registry import ModuleDef, ModuleKind

    return ModuleDef(
        id="notes",
        label="Web Notes",
        description=(
            "The sticky notes you keep on web pages with the NAVIG Dock — on this computer, "
            "searchable from the command line and by NAVIG's Recall at the level you allow."
        ),
        kind=ModuleKind.APP,
        category="tools",
        icon="sticky-note",
        capability=None,  # free
        surfaces=["dock:notes", "cli:notes"],
        requires=["gateway"],
        app_category="Knowledge",
        about=[
            "Notes you keep on a page, a whole site or everywhere, synced from the browser to "
            "this computer — nothing is uploaded.",
            "Search them, add one from the terminal (it appears on the page next time), export "
            "them, and let NAVIG find them when you ask.",
        ],
        features=[
            "Two-way sync with the NAVIG Dock (the newer revision always wins; deletes travel)",
            "Full-text search over titles, text, tags, pages and sites",
            f"Add a note from the terminal ({CMD} add)",
            "Sharing level: kept on this computer, or also usable by the agent",
        ],
        default_enabled=True,
        source="plugin",
    )


def register() -> None:
    """Register the free ``notes`` module and its deck routes (idempotent)."""
    global _REGISTERED
    if _REGISTERED:
        return
    try:
        from navig.modules.registry import register_module

        register_module(_notes_module_def())
    except Exception as exc:  # noqa: BLE001 — never break boot on a catalog hiccup
        _log.debug("notes module registration skipped: %s", exc)
    try:
        from navig.core.hooks import register_hook
    except Exception as exc:  # noqa: BLE001 — core too old / partial install
        _log.debug("notes: hook bus unavailable (%s)", exc)
        return
    register_hook("gateway:register_routes", _register_deck_routes)
    _REGISTERED = True


def _register_deck_routes(event) -> None:
    """Handler for ``gateway:register_routes`` — mounts the notes routes."""
    app = (getattr(event, "context", None) or {}).get("app")
    if app is None:
        return
    from navig_notes import deck_routes

    deck_routes.register(app)
