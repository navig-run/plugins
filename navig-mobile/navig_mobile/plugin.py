"""navig-mobile plugin entry point.

Mirrors the other first-party plugins (navig-antivirus / navig-email):

  * **CLI** — ``mobile`` / ``android`` / ``ios`` register via the
    ``navig.commands`` entry-point group (pyproject.toml) → ``navig mobile …``.
  * **Module catalog** — registers a FREE, toggleable ``mobile`` ModuleDef so the
    Store / desktop-OS / deck surfaces can show it. The device manager belongs on
    the **desktop** surface (``os-tile:mobile``), not the Telegram deck.
  * **OS routes** — the ``gateway:register_routes`` hook mounts the read-mostly
    ``/api/deck/mobile/*`` device-dashboard API. (``/api/deck/*`` is the core gateway
    namespace the **desktop OS** proxies — it is not the Telegram deck.) The visual
    view is the **Mobile** app in ``apps/os``; this plugin owns the backend contract.

All capabilities are FREE (no license capability). Registration failure never
blocks gateway boot or the CLI verbs (which mount independently).
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)
_REGISTERED = False


def _mobile_module_def():
    import dataclasses

    from navig.modules.registry import ModuleDef, ModuleKind

    kwargs = dict(
        id="mobile",
        label="Mobile",
        description="Android + iOS device operations — connect, apps, files, "
        "backups, forensics, spyware scan & rooting/jailbreak assist.",
        kind=ModuleKind.APP,
        category="operate",
        icon="smartphone",  # lucide icon
        capability=None,  # free — toggled via the module registry
        # Device manager is a desktop-OS app (Apps section), not a deck section.
        surfaces=["cli:mobile", "os-tile:mobile"],
        requires=["gateway"],
        default_enabled=True,
        source="plugin",
    )
    # The deck→desktop migration added scope/app_category to ModuleDef; only pass
    # them when the installed core supports them (forward/backward compatible).
    supported = {f.name for f in dataclasses.fields(ModuleDef)}
    if "scope" in supported:
        kwargs["scope"] = "brain"  # device inventory is per-machine, not per-space
    if "app_category" in supported:
        kwargs["app_category"] = "Systems"
    return ModuleDef(**kwargs)


def register() -> None:
    """Idempotently register the ``mobile`` module + gateway route hook. Called by
    core's ``load_entry_point_plugins()`` at gateway boot."""
    global _REGISTERED
    if _REGISTERED:
        return
    try:
        from navig.modules.registry import register_module

        register_module(_mobile_module_def())
    except Exception as exc:  # pragma: no cover — best-effort, never blocks boot
        _log.warning("navig-mobile: module registry unavailable (%s)", exc)

    try:
        from navig.core.hooks import register_hook
    except Exception as exc:  # pragma: no cover — core too old / partial install
        _log.warning("navig-mobile: hook bus unavailable (%s)", exc)
        _REGISTERED = True
        return

    register_hook("gateway:register_routes", _register_deck_routes)
    _REGISTERED = True


def _register_deck_routes(event) -> None:
    """Handler for ``gateway:register_routes`` — mounts the mobile deck routes."""
    app = (getattr(event, "context", None) or {}).get("app")
    if app is None:
        return
    try:
        from navig_mobile.deck_routes import mobile

        mobile.register(app)
    except Exception as exc:  # pragma: no cover — never block gateway boot
        _log.warning("navig-mobile: deck routes not mounted (%s)", exc)
