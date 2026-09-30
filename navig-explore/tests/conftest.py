"""Keep pytest's temp dirs inside the checkout's ``.dev/`` instead of the shared
per-user temp root.

By default pytest builds ``tmp_path`` under ``%LOCALAPPDATA%\\Temp\\pytest-of-<user>``
(or ``/tmp/pytest-of-<user>``). That directory lives outside the repo and is shared by
every tool on the machine. That shared ownership is not theoretical: on a machine here
it acquired a security descriptor its own owner could neither read, write nor delete,
and EVERY ``tmp_path`` test in this plugin — audio-sort, telegram-exports, route,
explorer, photos — failed with a ``PermissionError`` raised from deep inside a fixture,
which looks nothing like its cause and points at a directory nobody chose. (That
particular directory has since been repaired, which is precisely the point: the suite
should not have depended on machine state it does not own.)

Redirecting the temp root at import time (conftest is loaded before any fixture runs)
makes the suite depend only on the checkout it is running in, which is also where this
repo keeps generated dev artifacts. ``setdefault`` so an explicit ``--basetemp`` or a
caller-provided ``PYTEST_DEBUG_TEMPROOT`` still wins, and ``parents=True`` so it
self-heals on a fresh clone — pytest itself creates the base dir with ``parents=False``
and would otherwise fail on a path whose parents do not exist yet.
"""
from __future__ import annotations

import os
from pathlib import Path

_TEMP_ROOT = Path(__file__).resolve().parents[1] / ".dev" / "tmp"
_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("PYTEST_DEBUG_TEMPROOT", str(_TEMP_ROOT))
