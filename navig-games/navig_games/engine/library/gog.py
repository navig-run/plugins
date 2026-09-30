"""GOG Galaxy: installed games (and local cover-art URLs) from the galaxy-2.0.db
SQLite catalog, no login.

Joins InstalledBaseProducts (paths) → LimitedDetails (titles + images) → PlayTasks
+ PlayTaskLaunchParameters (the primary executable). The DB is copied to a *unique*
temp file before reading so a running Galaxy can't lock us out (and concurrent
scans don't clobber each other).
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
    base = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    return Path(base) / "GOG.com" / "Galaxy" / "storage" / "galaxy-2.0.db"


def _read(sql: str, params: tuple = (), db_path: Path | None = None) -> list[sqlite3.Row]:
    """Run a read-only query against a private copy of the Galaxy DB. Never raises."""
    db = db_path or _db_path()
    if not db.exists():
        return []
    fd, tmp_name = tempfile.mkstemp(prefix="navig_gog_", suffix=".db")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copy2(db, tmp)
        con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        return []
    finally:
        tmp.unlink(missing_ok=True)


def installed_gog_games(db_path: Path | None = None) -> list[InstalledGame]:
    rows = _read(
        """
        SELECT ibp.productId AS pid,
               ld.title      AS title,
               ibp.installationPath AS path,
               plp.executablePath   AS exe,
               plp.commandLineArgs   AS args
        FROM InstalledBaseProducts ibp
        LEFT JOIN LimitedDetails ld ON ld.productId = ibp.productId
        LEFT JOIN PlayTasks pt
               ON pt.gameReleaseKey = 'gog_' || ibp.productId
              AND pt.isPrimary = 1
        LEFT JOIN PlayTaskLaunchParameters plp ON plp.playTaskId = pt.id
        """,
        db_path=db_path,
    )
    games: list[InstalledGame] = []
    for r in rows:
        title = (r["title"] or "").strip()
        path = (r["path"] or "").strip()
        if not title or not path:
            continue
        games.append(
            InstalledGame(
                store="gog", title=title, app_id=str(r["pid"]),
                install_dir=path, launch_exe=(r["exe"] or "").strip(),
                launch_options=(r["args"] or "").strip(),
            )
        )
    return games


def gog_image_urls(product_id: str, db_path: Path | None = None) -> dict:
    """Local cover-art URLs for a GOG product (no key). Maps GOG's image roles to
    Steam grid roles: ``background`` → hero, ``logo`` → logo, ``icon`` → icon.

    GOG's LimitedDetails.images has no vertical/portrait cover, so the main grid
    tile still needs SteamGridDB or a manual image."""
    rows = _read(
        "SELECT images FROM LimitedDetails WHERE productId = ? LIMIT 1",
        (int(product_id),) if str(product_id).isdigit() else (product_id,),
        db_path=db_path,
    )
    if not rows or not rows[0]["images"]:
        return {}
    try:
        imgs = json.loads(rows[0]["images"])
    except (json.JSONDecodeError, TypeError):
        return {}
    out: dict[str, str] = {}
    if imgs.get("background"):
        out["hero"] = imgs["background"]
    if imgs.get("logo2x") or imgs.get("logo"):
        out["logo"] = imgs.get("logo2x") or imgs["logo"]
    if imgs.get("icon"):
        out["icon"] = imgs["icon"]
    return out
