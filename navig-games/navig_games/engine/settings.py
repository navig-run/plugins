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


def get(key: str, default: Any = None) -> Any:
    """Read ``plugins.games.<key>`` (best-effort; returns *default* on any error).

    navig's ConfigManager inside navig; without navig the same ``config.yaml`` (navig-sdk).
    """
    from navig_sdk.settings import get_path  # noqa: PLC0415

    try:
        return get_path(".".join((*_NS, key)), default)
    except Exception:  # noqa: BLE001 — an unreadable config reads as "not set"
        return default


def set(key: str, value: Any) -> None:  # noqa: A001 - deliberate config verb
    """Deep-set ``plugins.games.<key> = value`` and persist.

    Inside navig this is ``ConfigManager.set_global`` (refresh → deep-set → save). That
    matters here because there are **two writers**: the deck route runs inside the
    long-lived daemon (whose config snapshot goes stale the moment the user runs any
    `navig config set`) and the CLI runs in its own process. Writing a stale snapshot
    back used to erase the other one's settings. On its own it is the same read-modify-write
    of ``config.yaml``, which raises rather than overwrite a file it could not read.
    """
    from navig_sdk.settings import set_path  # noqa: PLC0415

    set_path(".".join((*_NS, key)), value)


def coerce_bool(value: Any, default: bool = False) -> bool:
    """Coerce a config value (possibly the raw string ``"false"``) to bool.

    Delegates to navig's canonical ``coerce_bool`` (via navig-sdk, which is navig's own when navig
    is installed and a parity-tested copy otherwise) so this
    plugin shares the one truth table instead of a divergent local copy.
    """
    from navig_sdk.host import coerce_bool as _canonical  # navig's own inside navig

    return _canonical(value, default)


def country(default: str = "US") -> str:
    val = get("country", default)
    return str(val or default).upper()


def locale(default: str = "en-US") -> str:
    return str(get("locale", default) or default)
