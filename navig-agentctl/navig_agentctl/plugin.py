"""navig-agentctl plugin entry point.

Wires the **Agent Control Center** (devops section) into a running NAVIG
daemon without any of it living in public core (mirrors ``navig-social``):

  * **CLI** — ``agentctl`` registers via the ``navig.commands`` entry-point
    group (pyproject.toml), discovered by ``navig.cli.registration``.
  * **Deck routes** — the Observer routes register via core's
    ``gateway:register_routes`` hook, fired after ``load_entry_point_plugins()``
    imports this module at gateway boot.
  * **Module catalog** — registers the FREE, toggleable ``agentctl`` ModuleDef
    that is the desktop app tile (``os-tile:agentctl``). ONE module owns the
    whole surface — tile + routes (``requires_module("agentctl")``) — so
    switching "Control Center" off disables all of it together.

The OS tile component ships in-tree in apps/os (plugins never ship frontend);
the tile goes live the moment the catalog carries this module.

Control Center is FREE (no license capability) — enable/disable is purely the
module registry override (``modules.overrides``).

Ownership contract with the forge session (navig-dev): this plugin owns the
`/api/deck/agents/*` contract + the session registry store. navig-dev is a
CONSUMER (read-only trees, heartbeat annotations) — never a second writer of
task state. See the Control Center plan for the full handoff.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)
_REGISTERED = False


def _agentctl_module_def():
    from navig.modules.registry import ModuleDef, ModuleKind

    return ModuleDef(
        id="agentctl",
        label="Control Center",
        description="Agent sessions, worktrees & tasks — one live view.",
        kind=ModuleKind.APP,
        category="operate",
        icon="workflow",
        capability=None,  # free — toggled via the module registry, not a license
        surfaces=["os-tile:agentctl", "cli:agentctl"],
        requires=["gateway"],
        default_enabled=True,
        source="plugin",
        app_category="Systems",
        about=[
            "The foreman's desk: every AI agent session, every git worktree and "
            "every task record in one read-only view — who owns what, where it "
            "runs, and what it is waiting on.",
            "Phase 1 observes only. Launching, assigning and recovery arrive in "
            "later phases — the view never changes what the workers do.",
        ],
        features=[
            "Live agent sessions with project, owner and idle/active verdicts",
            "Git worktrees: branch, ahead/behind, dirty state, conflict radar",
            "Unified tasks from the board and work queue: status, owner, dependencies",
        ],
    )


def register() -> None:
    """Idempotently register the ``agentctl`` module + the gateway route hook.

    Called by core's ``load_entry_point_plugins()`` at gateway boot.
    """
    global _REGISTERED
    if _REGISTERED:
        return

    try:
        from navig.modules.registry import register_module

        register_module(_agentctl_module_def())
    except Exception as exc:  # noqa: BLE001 — pragma: no cover
        _log.warning("navig-agentctl: module registry unavailable (%s)", exc)

    try:
        from navig.core.hooks import register_hook
    except Exception as exc:  # noqa: BLE001 — pragma: no cover — core too old
        _log.warning("navig-agentctl: hook bus unavailable (%s)", exc)
        return

    register_hook("gateway:register_routes", _register_deck_routes)
    _REGISTERED = True
    _log.info("navig-agentctl: Observer route hook + agentctl module registered")


def _register_deck_routes(event) -> None:
    """Handler for ``gateway:register_routes`` — mounts the Observer routes."""
    app = (getattr(event, "context", None) or {}).get("app")
    if app is None:
        return
    from navig_agentctl import deck_routes

    deck_routes.register(app)
    _log.debug("navig-agentctl: deck routes mounted")
