"""Cover art for non-Steam shortcuts — write the Steam grid-art files so unified
games look native in the library instead of showing a blank tile.

Steam reads per-user art from ``userdata/<id3>/config/grid/`` keyed by the 32-bit
shortcut appid:
  * ``<appid>p.<ext>``      portrait / library capsule (600×900) — the main tile
  * ``<appid>_hero.<ext>``  hero banner (1920×620)
  * ``<appid>_logo.<ext>``  clear logo
  * ``<appid>.<ext>``       horizontal capsule / header (460×215)
  * ``<appid>_icon.<ext>``  icon (also settable on the shortcut's ``icon`` field)

Art sources, in priority order: an explicit ``--image`` path · SteamGridDB (when a
key is configured) · the launcher's own local art (GOG gives hero + logo + icon,
keyless). Everything is best-effort — a network miss just leaves the tile blank.
Reference concept: SteamGridDB / BoilR.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..library.models import InstalledGame
from .shortcuts import shortcut_appid_for

_log = logging.getLogger(__name__)

# grid role → filename suffix (before the extension)
_SUFFIX = {"portrait": "p", "hero": "_hero", "logo": "_logo", "capsule": "", "icon": "_icon"}
_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/webp": ".webp"}


def grid_dir(id3: str | None = None) -> Path | None:
    from ..library.steam import active_user_id3, steam_path

    steam = steam_path()
    if steam is None:
        return None
    id3 = id3 or active_user_id3(steam)
    if not id3:
        return None
    d = steam / "userdata" / id3 / "config" / "grid"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ext_for(url: str, content_type: str = "") -> str:
    if content_type and content_type.split(";")[0].strip() in _EXT:
        return _EXT[content_type.split(";")[0].strip()]
    for e in (".png", ".jpg", ".jpeg", ".webp"):
        if url.lower().split("?")[0].endswith(e):
            return ".jpg" if e == ".jpeg" else e
    return ".png"


def _download(url: str, timeout: int = 20) -> tuple[bytes, str] | None:
    try:
        import requests

        r = requests.get(url, timeout=timeout, headers={"User-Agent": "navig-games"})
        r.raise_for_status()
        return r.content, r.headers.get("Content-Type", "")
    except Exception as exc:  # noqa: BLE001 — best-effort
        _log.debug("grid: download failed for %s (%s)", url, exc)
        return None


def _write(gdir: Path, appid: int, role: str, data: bytes, ext: str) -> Path:
    for stale in gdir.glob(f"{appid}{_SUFFIX[role]}.*"):  # replace any prior ext
        stale.unlink(missing_ok=True)
    dest = gdir / f"{appid}{_SUFFIX[role]}{ext}"
    dest.write_bytes(data)
    return dest


def _place_from_url(gdir: Path, appid: int, role: str, url: str) -> Path | None:
    got = _download(url)
    if not got:
        return None
    data, ctype = got
    return _write(gdir, appid, role, data, _ext_for(url, ctype))


def _place_from_file(gdir: Path, appid: int, role: str, path: Path) -> Path | None:
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    ext = Path(path).suffix.lower() or ".png"
    return _write(gdir, appid, role, data, ".jpg" if ext == ".jpeg" else ext)


def _steamgriddb_urls(name: str, key: str) -> dict:
    """Best-effort SteamGridDB lookup → {portrait, hero, logo}. Returns {} on any error."""
    try:
        import requests

        from urllib.parse import quote

        h = {"Authorization": f"Bearer {key}", "User-Agent": "navig-games"}
        # URL-encode the term — game names carry spaces / colons (e.g. "Half-Life 2").
        s = requests.get(
            f"https://www.steamgriddb.com/api/v2/search/autocomplete/{quote(name, safe='')}",
            headers=h, timeout=15)
        s.raise_for_status()
        data = s.json().get("data") or []
        if not data:
            return {}
        gid = data[0]["id"]
        out: dict[str, str] = {}
        for role, endpoint, params in (
            ("portrait", "grids", {"dimensions": "600x900"}),
            ("hero", "heroes", {}),
            ("logo", "logos", {}),
        ):
            try:
                r = requests.get(f"https://www.steamgriddb.com/api/v2/{endpoint}/game/{gid}",
                                 headers=h, params=params, timeout=15)
                items = r.json().get("data") or []
                if items:
                    out[role] = items[0]["url"]
            except Exception:  # noqa: BLE001
                continue
        return out
    except Exception as exc:  # noqa: BLE001
        _log.debug("grid: steamgriddb lookup failed for %s (%s)", name, exc)
        return {}


def _local_urls(game: InstalledGame) -> dict:
    """Keyless art from the launcher's own local data (GOG today)."""
    if game.store == "gog" and game.app_id:
        from ..library.gog import gog_image_urls

        return gog_image_urls(game.app_id)
    return {}


def apply_for_game(
    game: InstalledGame,
    *,
    id3: str | None = None,
    image: str | None = None,
    portrait: str | None = None,
    hero: str | None = None,
    logo: str | None = None,
    steamgriddb_key: str | None = None,
    use_local: bool = True,
) -> dict:
    """Resolve + write grid art for one game's shortcut. Returns which roles were set.

    Priority per role: explicit file (image/portrait/hero/logo) > SteamGridDB (if a
    key is given) > launcher-local art.
    """
    gdir = grid_dir(id3)
    if gdir is None:
        return {"ok": False, "error": "Steam grid folder not found"}
    appid = shortcut_appid_for(game)

    manual = {"portrait": portrait or image, "hero": hero, "logo": logo}
    remote = _steamgriddb_urls(game.title, steamgriddb_key) if steamgriddb_key else {}
    local = _local_urls(game) if use_local else {}

    set_roles: list[str] = []
    for role in ("portrait", "hero", "logo", "icon"):
        placed: Path | None = None
        if manual.get(role):
            placed = _place_from_file(gdir, appid, role, Path(manual[role]))
        elif remote.get(role):
            placed = _place_from_url(gdir, appid, role, remote[role])
        elif local.get(role):
            placed = _place_from_url(gdir, appid, role, local[role])
        if placed:
            set_roles.append(role)

    return {"ok": True, "game": game.title, "appid": appid, "set": set_roles,
            "grid_dir": str(gdir)}


def icon_path_for(game: InstalledGame, id3: str | None = None) -> str:
    """If an icon file was placed for this game, return its path (for the shortcut's
    ``icon`` field); else empty string."""
    gdir = grid_dir(id3)
    if gdir is None:
        return ""
    appid = shortcut_appid_for(game)
    for ext in (".png", ".jpg", ".ico", ".webp"):
        p = gdir / f"{appid}_icon{ext}"
        if p.exists():
            return str(p)
    return ""
