"""navig-email plugin entry point.

Wires the Email surface into a running NAVIG daemon (mirrors navig-social /
navig-harbor):

  * **CLI** — ``navig email`` registers via the ``navig.commands`` entry-point.
  * **Deck routes** — the Email routes register via core's ``gateway:register_routes``
    hook, each gated on the ``email`` module being enabled.
  * **Background tick** — the email filter→notify / briefing tick runs from core's
    notify scheduler, which soft-imports ``navig_email.service`` (skipped when this
    plugin isn't installed or the module is off).
  * **Module catalog** — registers a FREE, toggleable ``email`` ModuleDef.

Email is FREE (no license capability) — enable/disable is the module registry
override (``modules.overrides``).
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)
_REGISTERED = False


def _email_module_def():
    from navig.modules.registry import ModuleDef, ModuleKind

    return ModuleDef(
        id="email",
        label="Email",
        description="Email accounts, triage & send.",
        kind=ModuleKind.APP,
        category="operate",
        icon="mail",
        capability=None,  # free — toggled via the module registry
        # Desktop tile (deck→OS migration 2026-07-12; the deck's email app was
        # deleted — the OS Email app owns this surface now).
        surfaces=["os-tile:email", "cli:email"],
        requires=["gateway"],
        default_enabled=True,
        source="plugin",
        app_category="Comms",
        about=[
            "Gmail from the workbench, built on the native Gmail connector: "
            "search, read, reply, archive and send over OAuth — tokens stay "
            "encrypted in your local vault.",
            "Filter rules fire notifications through the unified notify system, "
            "and scheduled AI briefings digest your mail daily, weekly or monthly.",
        ],
        features=[
            "Inbox: Gmail search operators, read, reply, archive, delete",
            "Compose and send from the connected account",
            "Filter rules → deck / Telegram / email notifications",
            "AI briefings on a daily / weekly / monthly cadence",
        ],
        # Rendered by the desktop OS on apps/email/settings; values persist
        # through the generic per-app settings endpoint (apps.email.settings.*).
        settings_schema=[
            {
                "key": "default_query",
                "kind": "text",
                "label": "Default inbox search",
                "description": (
                    "The Gmail search the Inbox tab opens with — any Gmail "
                    "operator works (is:unread, from:someone, newer_than:30d)."
                ),
                "default": "newer_than:7d",
                "placeholder": "newer_than:7d",
                "max_length": 256,
            }
        ],
    )


def register() -> None:
    """Idempotently register the ``email`` module + gateway route hook. Called by
    core's ``load_entry_point_plugins()`` at gateway boot."""
    global _REGISTERED
    if _REGISTERED:
        return

    try:
        from navig.modules.registry import register_module

        register_module(_email_module_def())
    except Exception as exc:  # pragma: no cover
        _log.warning("navig-email: module registry unavailable (%s)", exc)

    try:
        from navig.core.hooks import register_hook
    except Exception as exc:  # pragma: no cover — core too old / partial install
        _log.warning("navig-email: hook bus unavailable (%s)", exc)
        return

    register_hook("gateway:register_routes", _register_deck_routes)
    _register_telegram_buttons()
    _REGISTERED = True
    _log.info("navig-email: Email route hook + email module registered")


def _register_telegram_buttons() -> None:
    """``mr:`` buttons (outbox and briefing controls). A core without plugin callbacks simply
    gets messages whose link buttons work and whose action buttons are not offered."""
    try:
        from navig.telegram.plugin_callbacks import register_callback_prefix
    except ImportError:
        _log.info("navig-email: this navig has no plugin Telegram buttons — link buttons only")
        return
    from navig_email import tg

    register_callback_prefix(tg.PREFIX, tg.handle, module="email")


def buttons_supported() -> bool:
    """Whether this navig can route plugin buttons (else only link buttons are sent).

    A capability check, not "registered in THIS process": a notification raised by the CLI
    is answered by the daemon, which registers the prefix at boot."""
    try:
        import navig.telegram.plugin_callbacks  # noqa: F401
    except ImportError:
        return False
    return True


def _register_deck_routes(event) -> None:
    """Handler for ``gateway:register_routes`` — mounts the Email deck routes."""
    app = (getattr(event, "context", None) or {}).get("app")
    if app is None:
        return
    from navig_email.deck_routes import email

    email.register(app)
    _log.debug("navig-email: Email deck routes mounted")
