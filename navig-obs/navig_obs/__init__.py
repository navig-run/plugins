"""navig-obs — control OBS Studio from navig over obs-websocket v5.

A standalone first-party plugin (lives in ``plugins/``, NOT in core). It mounts
as ``navig obs`` via the ``navig.commands`` entry point — no core edit — and
ships its own ``navig-obs`` console script (one command surface, two front doors).

Connection: flags ``--host/--port/--password`` > ``NAVIG_OBS_*`` env > navig
``obs.*`` config > OBS's own local websocket config (loopback only) > defaults
127.0.0.1:4455. The password is never printed or logged.

Verbs:
    navig obs status            # live state (skip + exit 0 when unreachable)
    navig obs doctor            # local health: install, websocket, plugins, missing files
    navig obs scenes list|create|remove|activate|current
    navig obs items list|visible|transform|remove|duplicate
    navig obs sources list|kinds|add|remove|settings|active|defaults
    navig obs filters list|add|remove|settings|enable
    navig obs transitions list|current|set|settings
    navig obs stream status|start|stop|pause|resume|settings
    navig obs record status|start|stop|pause|resume|directory
    navig obs replay status|start|stop|save
    navig obs vcam status|start|stop
    navig obs studio status|enable|disable|to-program
    navig obs collections list|current|create|use
    navig obs profiles list|current|use
    navig obs media status|play|pause|restart|stop
    navig obs hotkeys list|send
    navig obs screenshot <source> [--out file]
"""

from __future__ import annotations

__version__ = "0.1.0"
