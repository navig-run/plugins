"""The navig core helpers this plugin uses that are newer than its declared floor.

navig-cabinet declares ``navig>=3.25.0`` — the core on PyPI. A few helpers it uses landed
in core after that release. Importing them directly would make the plugin's floor a false
claim: installed next to navig 3.25.0 it would raise ImportError the first time the
feature ran (`scripts/check_plugin_core_floor.py` measures exactly that).

So each is imported here, guarded by ``except ImportError``, with a fallback that either
does the same job (the pure helpers are copied, behaviour-identical) or reports plainly
that the feature needs a newer navig core. Nothing degrades silently. When the floor is
raised past the release that contains them, this module can shrink to plain re-exports.
"""

from __future__ import annotations

import html
import os
from pathlib import Path
from typing import Any

NEWER_CORE = "a newer navig core than 3.25.0 (pip install -U navig)"

# ── paths: resolve what the operator TYPED against where they typed it ──────────
try:
    from navig.platform.paths import resolve_user_path
except ImportError:  # navig <= 3.25.0 — copied from core, same behaviour

    def resolve_user_path(given: Path | str) -> Path:
        """Absolute as given; relative anchored to the directory navig was run from.

        navig chdirs into the active space before a command runs and stashes the real
        invocation directory in ``NAVIG_INVOCATION_CWD``; resolving against the process
        cwd would silently answer a different folder.
        """
        p = Path(given).expanduser()
        if p.is_absolute():
            return p.resolve()
        raw = os.environ.get("NAVIG_INVOCATION_CWD")
        origin = Path(raw) if raw and Path(raw).is_dir() else Path.cwd()
        return (origin / p).resolve()


# ── config coercion ─────────────────────────────────────────────────────────
try:
    from navig.core.coerce import coerce_int
except ImportError:  # navig <= 3.25.0 — same contract as core's

    def coerce_int(value: Any, default: int, *, minimum: int | None = None) -> int:
        """A config value as an int (``navig config set`` stores strings), else *default*."""
        if isinstance(value, bool):
            return default
        try:
            n = int(str(value).strip()) if value is not None else default
        except (TypeError, ValueError):
            n = default
        return max(n, minimum) if minimum is not None else n


# ── Telegram notify (paperwork --send) ──────────────────────────────────────
# Looked up on every call, like the direct lazy imports they replace: a test (or a
# caller) that patches navig.messaging.notify_operator must still be the one used.


def notify_available() -> bool:
    try:
        import navig.messaging.notify_operator  # noqa: F401
    except ImportError:
        return False
    return True


def notify_operator(text: str) -> bool:
    """Send to the operator's Telegram; False when not sent — including on a core that
    predates the notifier, so ``--send`` reports "not sent" instead of raising."""
    try:
        from navig.messaging import notify_operator as _n
    except ImportError:
        return False
    return _n.notify_operator(text)


def escape_html(text: str) -> str:
    try:
        from navig.messaging.notify_operator import escape_html as _esc
    except ImportError:
        return html.escape(str(text), quote=False)
    return _esc(text)


# ── the guarded LLM door (paperwork reply) ──────────────────────────────────


def llm_guard():
    """``navig.llm.guard``, or None when this core predates it."""
    try:
        from navig.llm import guard
    except ImportError:
        return None
    return guard
