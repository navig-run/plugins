"""Add non-Steam games to the Steam library by editing ``shortcuts.vdf``.

Safe by construction: backs up the existing file, is idempotent (dedupes by Exe),
and refuses to write while Steam is running (Steam rewrites the file on exit,
which would silently drop our additions) unless forced.
"""

from __future__ import annotations

import shutil
import time
from collections import OrderedDict
from pathlib import Path

from ..library.models import InstalledGame
from ..library.steam import active_user_id3, is_steam_running, shortcuts_path, steam_path
from . import vdf

# Fields Steam expects on a shortcut entry (order preserved on write).
_ENTRY_DEFAULTS = OrderedDict(
    [
        ("IsHidden", 0), ("AllowDesktopConfig", 1), ("AllowOverlay", 1),
        ("OpenVR", 0), ("Devkit", 0), ("DevkitGameID", ""),
        ("DevkitOverrideAppID", 0), ("LastPlayTime", 0), ("FlatpakAppID", ""),
    ]
)


def _quote(path: str) -> str:
    path = path.strip().strip('"')
    return f'"{path}"' if path else ""


def shortcut_appid_for(game: InstalledGame) -> int:
    """The unsigned 32-bit Steam shortcut appid for a game — the single source of
    truth shared by the shortcut entry and its grid-art file names."""
    return vdf.shortcut_appid(_quote(game.launch_exe), game.title)


def _entry(game: InstalledGame, tag: str, icon: str = "") -> "OrderedDict":
    exe = _quote(game.launch_exe)
    start_dir = _quote(str(Path(game.launch_exe).parent) if game.launch_exe else game.install_dir)
    e: OrderedDict = OrderedDict()
    e["appid"] = shortcut_appid_for(game)
    e["AppName"] = game.title
    e["Exe"] = exe
    e["StartDir"] = start_dir
    e["icon"] = icon
    e["ShortcutPath"] = ""
    e["LaunchOptions"] = game.launch_options or ""
    for k, v in _ENTRY_DEFAULTS.items():
        e[k] = v
    e["tags"] = OrderedDict([("0", tag)])
    return e


def load_shortcuts(path: Path) -> "OrderedDict":
    """Return the ``shortcuts`` entries map (empty if the file is missing/new)."""
    if not path.exists():
        return OrderedDict()
    try:
        root = vdf.read_binary(path.read_bytes())
    except (ValueError, OSError):
        return OrderedDict()
    got = root.get("shortcuts", OrderedDict())
    return got if isinstance(got, OrderedDict) else OrderedDict(got)


def _existing_exes(entries: OrderedDict) -> set[str]:
    out = set()
    for e in entries.values():
        if isinstance(e, dict) and e.get("Exe"):
            out.add(str(e["Exe"]).strip().strip('"').lower())
    return out


def present_exes(id3: str | None = None) -> set[str]:
    """Lowercased executables already present as non-Steam shortcuts (for the
    caller to skip re-adding / re-arting them). Empty on any failure."""
    steam = steam_path()
    if steam is None:
        return set()
    id3 = id3 or active_user_id3(steam)
    if not id3:
        return set()
    return _existing_exes(load_shortcuts(shortcuts_path(steam, id3)))


def add_games(
    games: list[InstalledGame],
    *,
    id3: str | None = None,
    tag: str = "navig",
    dry_run: bool = False,
    force: bool = False,
    icons: dict[str, str] | None = None,
) -> dict:
    """Add each launchable game as a non-Steam shortcut. Returns a summary dict.

    Skips games already present (by executable) and non-launchable games. Writes
    atomically after backing up the previous shortcuts.vdf. ``icons`` optionally
    maps ``game.key`` → an icon file path to set on the shortcut.
    """
    icons = icons or {}
    steam = steam_path()
    if steam is None:
        return {"ok": False, "error": "Steam not found on this machine"}

    id3 = id3 or active_user_id3(steam)
    if not id3:
        return {"ok": False, "error": "could not determine the active Steam user (userdata id)"}

    if is_steam_running() and not force:
        return {"ok": False, "error": "Steam is running — quit Steam first (or pass force=True); "
                                      "Steam overwrites shortcuts.vdf on exit"}

    path = shortcuts_path(steam, id3)
    entries = load_shortcuts(path)
    have = _existing_exes(entries)

    added, skipped_present, skipped_unlaunchable = [], [], []
    # Next numeric index for new entries.
    idx = max((int(k) for k in entries.keys() if str(k).isdigit()), default=-1) + 1
    for game in games:
        if not game.launchable:
            skipped_unlaunchable.append(game.title)
            continue
        if _quote(game.launch_exe).strip('"').lower() in have:
            skipped_present.append(game.title)
            continue
        entries[str(idx)] = _entry(game, tag, icon=_quote(icons.get(game.key, "")))
        have.add(game.launch_exe.strip().lower())
        added.append(game.title)
        idx += 1

    summary = {
        "ok": True, "user": id3, "path": str(path),
        "added": added, "skipped_present": skipped_present,
        "skipped_unlaunchable": skipped_unlaunchable, "dry_run": dry_run,
        "total_shortcuts": len(entries),
    }
    if dry_run or not added:
        return summary

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_suffix(f".vdf.bak-{int(time.time())}")
        try:
            shutil.copy2(path, backup)
            summary["backup"] = str(backup)
        except OSError:
            pass
    data = vdf.write_binary(OrderedDict([("shortcuts", entries)]))
    tmp = path.with_suffix(".vdf.tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return summary


def list_shortcuts(id3: str | None = None) -> list[dict]:
    """List the current non-Steam shortcuts (name + exe + tags)."""
    steam = steam_path()
    if steam is None:
        return []
    id3 = id3 or active_user_id3(steam)
    if not id3:
        return []
    entries = load_shortcuts(shortcuts_path(steam, id3))
    out = []
    for e in entries.values():
        if not isinstance(e, dict):
            continue
        tags = e.get("tags", {})
        out.append({
            "name": e.get("AppName", ""),
            "exe": str(e.get("Exe", "")).strip('"'),
            "tags": list(tags.values()) if isinstance(tags, dict) else [],
        })
    return out
