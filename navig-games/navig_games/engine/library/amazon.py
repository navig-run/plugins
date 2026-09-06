"""Amazon Games: installed games from the local SQLite install catalog, no login.

%LOCALAPPDATA%\\Amazon Games\\Data\\Games\\Sql\\GameInstallInfo.sqlite holds the
DbSet with ProductTitle / InstallDirectory. Executable is resolved from the
per-game fuel.json when present.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

from .models import InstalledGame


def _db_path() -> Path:
    base = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
    return Path(base) / "Amazon Games" / "Data" / "Games" / "Sql" / "GameInstallInfo.sqlite"


def _resolve_exe(install_dir: str) -> str:
    """Amazon games ship a fuel.json describing the launch command."""
    fuel = Path(install_dir) / "fuel.json"
    if not fuel.exists():
        return ""
    try:
        data = json.loads(fuel.read_text(encoding="utf-8", errors="replace"))
        cmd = (data.get("Main") or {}).get("Command", "")
        if cmd:
            return str(Path(install_dir) / cmd)
    except (json.JSONDecodeError, OSError):
        pass
    return ""


def installed_amazon_games(db_path: Path | None = None) -> list[InstalledGame]:
    db = db_path or _db_path()
    if not db.exists():
        return []
    # A UNIQUE temp file, exactly as gog.py does it. This used to be a fixed
    # `navig_amazon_games.db` in the shared temp dir, so two overlapping calls — the daemon
    # and a CLI run, or two `navig games` invocations — shared one path: each `copy2` wrote
    # over the copy the other was reading, and the `finally` below deleted it out from under
    # them. Both failures land in `except OSError: return []`, so the symptom was "you own no
    # Amazon games" rather than an error. A predictable path in a shared temp dir is also
    # another user's to create first.
    fd, tmp_name = tempfile.mkstemp(prefix="navig_amazon_", suffix=".db")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copy2(db, tmp)
        con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        # Column names vary across client versions — discover them.
        cols = {c[1].lower() for c in con.execute("PRAGMA table_info(DbSet)")}
        title_col = "ProductTitle" if "producttitle" in cols else "productTitle"
        dir_col = "InstallDirectory" if "installdirectory" in cols else "installDirectory"
        id_col = "Id" if "id" in cols else "ProductIdStr"
        rows = con.execute(
            f'SELECT "{title_col}" AS title, "{dir_col}" AS dir, "{id_col}" AS id FROM DbSet'
        ).fetchall()
        con.close()
    except (sqlite3.Error, OSError):
        return []
    finally:
        tmp.unlink(missing_ok=True)

    games: list[InstalledGame] = []
    for r in rows:
        title = (r["title"] or "").strip()
        install = (r["dir"] or "").strip()
        if not title or not install:
            continue
        games.append(
            InstalledGame(
                store="amazon", title=title, app_id=str(r["id"] or ""),
                install_dir=install, launch_exe=_resolve_exe(install),
            )
        )
    return games
