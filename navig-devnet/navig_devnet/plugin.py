"""navig-devnet plugin entry point.

Registers:
  * **CLI** — ``devnet`` via the ``navig.commands`` entry-point group (pyproject).
  * **Module catalog** — a FREE, toggleable ``devnet`` ModuleDef, surfacing on
    ``cli:devnet`` and ``toolset:devnet`` so per-space activation gates BOTH the
    CLI verb and the agent tools (see per-space module activation).
  * **Agent tools** — ``net_dns`` / ``net_ssl`` / ``net_whois`` (toolset ``devnet``).

The ``devnet`` module id deliberately matches the ``devnet`` CLI verb, so the
per-space CLI gate (which matches by command name) scopes it with no extra map.
Lookups are read-only and FREE; the authorization-gated active tooling (scanning,
intercepting proxy) arrives in a later phase as DANGEROUS + owner_only, gated
independently of this module toggle.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)
_REGISTERED = False


def _devnet_module_def():
    from navig.modules.registry import ModuleDef, ModuleKind

    return ModuleDef(
        id="devnet",
        label="DevNet",
        description="Developer/ops network inspection: DNS, WHOIS, SSL lookups.",
        kind=ModuleKind.APP,
        category="build",
        icon="network",
        capability=None,  # free
        surfaces=["cli:devnet", "toolset:devnet", "os-tile:devnet"],
        default_enabled=True,
        app_category="Systems",
        source="plugin",
    )


def register() -> None:
    """Idempotently register the ``devnet`` module + agent tools. Called by core's
    ``load_entry_point_plugins()``."""
    global _REGISTERED
    if _REGISTERED:
        return
    try:
        from navig.modules.registry import register_module

        register_module(_devnet_module_def())
    except Exception as exc:  # pragma: no cover
        _log.warning("navig-devnet: module registry unavailable (%s)", exc)
    try:
        from navig_devnet.agent_tools import register_devnet_tools

        register_devnet_tools()
    except Exception as exc:  # noqa: BLE001 — never block boot
        _log.warning("navig-devnet: agent tools not registered (%s)", exc)
    try:
        from navig.core.hooks import register_hook

        register_hook("gateway:register_routes", _register_deck_routes)
    except Exception as exc:  # noqa: BLE001 — a CLI process has no gateway; harmless
        _log.warning("navig-devnet: gateway route hook not registered (%s)", exc)
    _REGISTERED = True


def _register_deck_routes(event) -> None:
    """Handler for ``gateway:register_routes`` — mounts the DevNet deck API
    (gated on the ``devnet`` module being enabled)."""
    app = (getattr(event, "context", None) or {}).get("app")
    if app is None:
        return
    try:
        from navig_devnet.deck_routes import devnet as devnet_routes

        devnet_routes.register(app)
        _log.debug("navig-devnet: deck API routes mounted")
    except Exception as exc:  # noqa: BLE001
        _log.warning("navig-devnet: could not mount deck routes (%s)", exc)
