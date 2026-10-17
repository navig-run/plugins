"""navig-calendar plugin entry point.

The "connector-backed capability" exemplar: the CLI moved out of core, while
the calendar *providers* (GoogleCalendar / CalDAVProvider / ICSCalendarProvider
in ``navig.agent.proactive``) stay in core — they're shared with the proactive
assistant. The teaching point of this extraction: shared helpers move DOWN into
core; the CLI moves OUT into the plugin.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)
_REGISTERED = False


def _calendar_module_def():
    from navig.modules.registry import ModuleDef, ModuleKind

    return ModuleDef(
        id="calendar",
        label="Calendar",
        description="Calendar events — list, add, sync (Google / CalDAV / ICS).",
        kind=ModuleKind.APP,
        category="grow",
        icon="calendar",
        capability=None,  # free
        surfaces=["cli:calendar"],
        default_enabled=True,
        source="plugin",
    )


def register() -> None:
    """Idempotently register the ``calendar`` module. Called by core's
    ``load_entry_point_plugins()``."""
    global _REGISTERED
    if _REGISTERED:
        return
    try:
        from navig.modules.registry import register_module

        register_module(_calendar_module_def())
    except Exception as exc:  # pragma: no cover
        _log.warning("navig-calendar: module registry unavailable (%s)", exc)
    _REGISTERED = True
