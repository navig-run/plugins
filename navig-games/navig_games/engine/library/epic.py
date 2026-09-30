"""Epic Games Launcher: installed games from the .item manifests (JSON), no login.

Manifests live at %ProgramData%\\Epic\\EpicGamesLauncher\\Data\\Manifests\\*.item.
"""

from __future__ import annotations

import glob
import json
import os
from pathlib import Path

from .models import InstalledGame


def _manifests_dir() -> Path:
    base = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    return Path(base) / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"


def installed_epic_games(manifests_dir: Path | None = None) -> list[InstalledGame]:
    d = manifests_dir or _manifests_dir()
    if not d.exists():
        return []
    games: list[InstalledGame] = []
    for item in glob.glob(str(d / "*.item")):
        try:
            data = json.loads(Path(item).read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError):
            continue
        title = (data.get("DisplayName") or "").strip()
        install = (data.get("InstallLocation") or "").strip()
        exe = (data.get("LaunchExecutable") or "").strip()
        if not title or not install:
            continue
        launch_exe = str(Path(install) / exe) if exe else ""
        games.append(
            InstalledGame(
                store="epic", title=title,
                app_id=str(data.get("AppName") or data.get("CatalogItemId") or ""),
                install_dir=install, launch_exe=launch_exe,
                launch_options=(data.get("LaunchCommand") or "").strip(),
            )
        )
    return games
