"""Steam: install location, library folders, installed games, and the active
user's userdata id (SteamID3) — all from on-disk VDF, no login."""

from __future__ import annotations

import glob
from pathlib import Path

from ..steam import vdf
from .models import InstalledGame

_STEAMID64_BASE = 76561197960265728  # SteamID64 - this == SteamID3 (userdata dir)

# Steam "apps" that are runtimes/tools, not games — hidden from the library view.
_SKIP_STEAM_APPIDS = {"228980", "1070560", "1391110", "1628350"}  # redistributables/runtimes


def steam_path() -> Path | None:
    """Locate the Steam install dir (registry first, then common paths)."""
    try:
        import winreg

        for hive, key, val in (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        ):
            try:
                with winreg.OpenKey(hive, key) as k:
                    p = Path(winreg.QueryValueEx(k, val)[0])
                    if p.exists():
                        return p
            except OSError:
                continue
    except Exception:  # noqa: BLE001 — non-Windows or no registry
        pass
    for c in (
        Path(r"C:\Program Files (x86)\Steam"),
        Path(r"C:\Program Files\Steam"),
        Path.home() / ".steam" / "steam",
        Path.home() / ".local" / "share" / "Steam",
    ):
        if c.exists():
            return c
    return None


def _load_kv(path: Path) -> dict:
    try:
        return vdf.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return {}


def library_folders(steam: Path) -> list[Path]:
    """All Steam library roots (each contains a ``steamapps`` dir)."""
    libs: list[Path] = [steam]
    data = _load_kv(steam / "steamapps" / "libraryfolders.vdf").get("libraryfolders", {})
    for _, entry in data.items() if isinstance(data, dict) else []:
        if isinstance(entry, dict) and entry.get("path"):
            p = Path(entry["path"])
            if p not in libs:
                libs.append(p)
    return [lib for lib in libs if lib.exists()]


def installed_steam_games(steam: Path | None = None) -> list[InstalledGame]:
    steam = steam or steam_path()
    if steam is None:
        return []
    games: list[InstalledGame] = []
    for lib in library_folders(steam):
        appsdir = lib / "steamapps"
        for acf in glob.glob(str(appsdir / "appmanifest_*.acf")):
            state = _load_kv(Path(acf)).get("AppState", {})
            if not isinstance(state, dict):
                continue
            appid = str(state.get("appid", "")).strip()
            name = str(state.get("name", "")).strip()
            installdir = str(state.get("installdir", "")).strip()
            if not appid or not name:
                continue
            if appid in _SKIP_STEAM_APPIDS or "redistributable" in name.lower():
                continue  # runtime/tool, not a game
            game_dir = appsdir / "common" / installdir if installdir else appsdir
            games.append(
                InstalledGame(
                    store="steam", title=name, app_id=appid,
                    install_dir=str(game_dir),
                    # Steam games launch via steam://; not added back to Steam.
                    launch_options=f"steam://rungameid/{appid}",
                )
            )
    return games


def login_users(steam: Path) -> list[dict]:
    """Parse loginusers.vdf → [{steamid64, account, persona, most_recent, id3}]."""
    users = _load_kv(steam / "config" / "loginusers.vdf").get("users", {})
    out: list[dict] = []
    for sid64, info in users.items() if isinstance(users, dict) else []:
        if not isinstance(info, dict):
            continue
        try:
            id3 = int(sid64) - _STEAMID64_BASE
        except ValueError:
            continue
        out.append({
            "steamid64": sid64,
            "id3": str(id3),
            "account": info.get("AccountName", ""),
            "persona": info.get("PersonaName", ""),
            "most_recent": str(info.get("MostRecent", "0")) == "1",
        })
    return out


def active_user_id3(steam: Path | None = None) -> str | None:
    """The SteamID3 (userdata folder name) of the most-recently-logged-in user."""
    steam = steam or steam_path()
    if steam is None:
        return None
    users = login_users(steam)
    if not users:
        # Fall back to the sole userdata dir if there's exactly one.
        dirs = userdata_dirs(steam)
        return dirs[0].name if len(dirs) == 1 else None
    for u in users:
        if u["most_recent"]:
            return u["id3"]
    return users[0]["id3"]


def userdata_dirs(steam: Path) -> list[Path]:
    ud = steam / "userdata"
    return [d for d in ud.iterdir() if d.is_dir()] if ud.exists() else []


def shortcuts_path(steam: Path, id3: str) -> Path:
    return steam / "userdata" / id3 / "config" / "shortcuts.vdf"


def is_steam_running() -> bool:
    """Best-effort: is a steam.exe process running? (write shortcuts only when not)."""
    try:
        import subprocess

        # BYTES, not text=True: tasklist writes the OEM code page while text=True decodes
        # with the ANSI one, and its output is not pure ASCII. "steam.exe" is, so match
        # without decoding — a strict encoding= would raise on the localized header and
        # this except: would turn that into "Steam is not running", which is the answer
        # that decides whether shortcuts get overwritten under a live client.
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq steam.exe", "/NH"],
            capture_output=True, timeout=5,
        )
        return b"steam.exe" in out.stdout.lower()
    except Exception:  # noqa: BLE001 — non-Windows / tasklist absent
        return False
