"""Cross-launcher installed-game discovery (Windows-native, manifest-based — no
launcher APIs, no login). Feeds Steam unification.
"""

from __future__ import annotations

import logging

from .models import InstalledGame

_log = logging.getLogger(__name__)

ALL_STORES = ("steam", "epic", "gog", "amazon")


def scan(stores: list[str] | None = None) -> list[InstalledGame]:
    """Scan installed games across the given stores (default: all). Never raises;
    a failing scanner contributes nothing and is logged."""
    stores = stores or list(ALL_STORES)
    games: list[InstalledGame] = []
    scanners = {
        "steam": _safe(_steam),
        "epic": _safe(_epic),
        "gog": _safe(_gog),
        "amazon": _safe(_amazon),
    }
    for store in stores:
        fn = scanners.get(store)
        if fn:
            games.extend(fn())
    return games


def scan_non_steam(stores: list[str] | None = None) -> list[InstalledGame]:
    """Everything except Steam — the candidates for adding to the Steam library."""
    stores = [s for s in (stores or ALL_STORES) if s != "steam"]
    return scan(stores)


def _safe(fn):
    def wrapped() -> list[InstalledGame]:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            _log.warning("games.library: %s scan failed (%s)", fn.__name__, exc)
            return []

    return wrapped


def _steam() -> list[InstalledGame]:
    from .steam import installed_steam_games

    return installed_steam_games()


def _epic() -> list[InstalledGame]:
    from .epic import installed_epic_games

    return installed_epic_games()


def _gog() -> list[InstalledGame]:
    from .gog import installed_gog_games

    return installed_gog_games()


def _amazon() -> list[InstalledGame]:
    from .amazon import installed_amazon_games

    return installed_amazon_games()
