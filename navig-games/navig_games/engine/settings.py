"""Config read/write for the games plugin — namespace ``plugins.games.*``.

Follows the NAVIG deep-set pattern (see CLAUDE.md "Sharp edges" and
``navig_social/social/oauth.py:set_config``): deep-set the leaf on the live
config dict then re-save. Never use ``update_global_config`` for nested keys
(shallow ``.update`` clobbers the subtree) and never call a non-existent
``ConfigManager.set``. Values written by ``navig config set`` come back as raw
strings, so booleans are coerced.
"""

from __future__ import annotations

from typing import Any

_NS = ("plugins", "games")


def _cm():
    from navig.config import get_config_manager

    return get_config_manager()


def get(key: str, default: Any = None) -> Any:
    """Read ``plugins.games.<key>`` (best-effort; returns *default* on any error)."""
    try:
        node: Any = _cm().global_config
    except Exception:
        return default
    for part in _NS:
        if not isinstance(node, dict):
            return default
        node = node.get(part)
        if node is None:
            return default
    if isinstance(node, dict):
        return node.get(key, default)
    return default


def set(key: str, value: Any) -> None:  # noqa: A001 - deliberate config verb
    """Deep-set ``plugins.games.<key> = value`` and persist.

    Goes through ``ConfigManager.set_global`` (refresh → deep-set → save). That
    matters here because there are **two writers**: the deck route runs inside the
    long-lived daemon (whose config snapshot goes stale the moment the user runs any
    `navig config set`) and the CLI runs in its own process. Writing a stale snapshot
    back used to erase the other one's settings.
    """
    cm = _cm()
    setter = getattr(cm, "set_global", None)
    if callable(setter):  # core ≥ the concurrent-write fix
        setter(".".join((*_NS, key)), value)
        return

    # Fallback for an older core: refresh what we can, then deep-set + save.
    node = cm.global_config
    for part in _NS:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[key] = value
    cm._save_global_config(cm.global_config)


def coerce_bool(value: Any, default: bool = False) -> bool:
    """Coerce a config value (possibly the raw string ``"false"``) to bool.

    Delegates to core's canonical :func:`navig.core.coerce.coerce_bool` so this
    plugin shares the one truth table instead of a divergent local copy.
    """
    from navig.core.coerce import coerce_bool as _canonical

    return _canonical(value, default)


def country(default: str = "US") -> str:
    val = get("country", default)
    return str(val or default).upper()


def locale(default: str = "en-US") -> str:
    return str(get("locale", default) or default)
