"""navig-chats plugin entry point.

* **CLI** — ``chats`` registers via the ``navig.commands`` entry-point group.
* **Module catalog** — a FREE, toggleable ``chats`` ModuleDef, so the routes have a gate.
* **Deck routes** — ``/api/deck/chats/*`` for the Dock's ChatGPT Lens and library page,
  mounted on the ``gateway:register_routes`` hook. They answer ONLY this computer; see
  :mod:`navig_chats.deck_routes`.
"""

from __future__ import annotations

import logging

from navig_sdk.host import command_name

CMD = command_name("chats", standalone="navig-chats")

_log = logging.getLogger(__name__)
_REGISTERED = False


def _chats_module_def():
    from navig.modules.registry import ModuleDef, ModuleKind

    return ModuleDef(
        id="chats",
        label="Chat Library",
        description=(
            "Every AI conversation you choose to keep — imported from your ChatGPT export or "
            "saved while you browse — searchable down to the message, in folders, exportable."
        ),
        kind=ModuleKind.APP,
        category="tools",
        icon="messages-square",
        capability=None,  # free
        surfaces=["dock:chat-library", "cli:chats"],
        requires=["gateway"],
        app_category="Knowledge",
        about=[
            "Your ChatGPT history on this computer: import the account export, or save a chat "
            "from the page with one click.",
            "Search every message, open the exact match back in ChatGPT, and keep chats in your "
            "own folders — a local overlay that never changes ChatGPT.",
        ],
        features=[
            "Full-text search with phrases, exclusions and prefixes",
            "Folders, tags, pins and local renames",
            "Export to Markdown, HTML, JSON or a zip with checksums",
            f"Import your ChatGPT data export ({CMD} import)",
        ],
        default_enabled=True,
        source="plugin",
    )


def register() -> None:
    """Register the free ``chats`` module and its deck routes (idempotent)."""
    global _REGISTERED
    if _REGISTERED:
        return
    try:
        from navig.modules.registry import register_module

        register_module(_chats_module_def())
    except Exception as exc:  # noqa: BLE001 — never break boot on a catalog hiccup
        _log.debug("chats module registration skipped: %s", exc)
    try:
        from navig.core.hooks import register_hook
    except Exception as exc:  # noqa: BLE001 — core too old / partial install
        _log.debug("chats: hook bus unavailable (%s)", exc)
        return
    register_hook("gateway:register_routes", _register_deck_routes)
    _REGISTERED = True


def _register_deck_routes(event) -> None:
    """Handler for ``gateway:register_routes`` — mounts the chat library routes."""
    app = (getattr(event, "context", None) or {}).get("app")
    if app is None:
        return
    from navig_chats import deck_routes

    deck_routes.register(app)
