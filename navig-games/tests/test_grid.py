"""Steam grid cover art — extension detection, placement, and GOG art extraction.

No network: manual-file placement + a fixture GOG DB cover the logic; the download
path is best-effort and exercised live.
"""

import json
import sqlite3

from navig_games.engine.library import gog as gog_lib
from navig_games.engine.library.models import InstalledGame
from navig_games.engine.steam import grid
from navig_games.engine.steam.shortcuts import shortcut_appid_for

_PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def _game() -> InstalledGame:
    return InstalledGame(store="gog", title="Cool", app_id="111", launch_exe="C:/g/cool.exe")


def test_ext_for():
    assert grid._ext_for("http://x/a.jpg") == ".jpg"
    assert grid._ext_for("http://x/a.png?token=1") == ".png"
    assert grid._ext_for("http://x/a.jpeg") == ".jpg"  # normalized
    assert grid._ext_for("http://x/a", "image/jpeg") == ".jpg"
    assert grid._ext_for("http://x/no-ext") == ".png"  # default


def test_apply_manual_portrait(tmp_path, monkeypatch):
    gdir = tmp_path / "grid"
    gdir.mkdir()
    monkeypatch.setattr(grid, "grid_dir", lambda id3=None: gdir)
    src = tmp_path / "cover.png"
    src.write_bytes(_PNG)

    g = _game()
    r = grid.apply_for_game(g, portrait=str(src), use_local=False)
    assert r["ok"] and r["set"] == ["portrait"]
    assert (gdir / f"{shortcut_appid_for(g)}p.png").exists()


def test_image_is_alias_for_portrait(tmp_path, monkeypatch):
    gdir = tmp_path / "grid"
    gdir.mkdir()
    monkeypatch.setattr(grid, "grid_dir", lambda id3=None: gdir)
    src = tmp_path / "c.png"
    src.write_bytes(_PNG)
    r = grid.apply_for_game(_game(), image=str(src), use_local=False)
    assert "portrait" in r["set"]


def test_apply_replaces_stale_extension(tmp_path, monkeypatch):
    gdir = tmp_path / "grid"
    gdir.mkdir()
    monkeypatch.setattr(grid, "grid_dir", lambda id3=None: gdir)
    g = _game()
    appid = shortcut_appid_for(g)
    (gdir / f"{appid}p.jpg").write_bytes(b"stale")  # prior art in a different ext
    src = tmp_path / "c.png"
    src.write_bytes(_PNG)

    grid.apply_for_game(g, portrait=str(src), use_local=False)
    assert not (gdir / f"{appid}p.jpg").exists()  # stale removed
    assert (gdir / f"{appid}p.png").exists()  # replaced


def test_icon_path_for(tmp_path, monkeypatch):
    gdir = tmp_path / "grid"
    gdir.mkdir()
    monkeypatch.setattr(grid, "grid_dir", lambda id3=None: gdir)
    g = _game()
    assert grid.icon_path_for(g) == ""  # nothing placed yet
    (gdir / f"{shortcut_appid_for(g)}_icon.png").write_bytes(_PNG)
    assert grid.icon_path_for(g).endswith("_icon.png")


def test_gog_image_urls(tmp_path):
    db = tmp_path / "g.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE LimitedDetails(productId INTEGER, images TEXT)")
    con.execute(
        "INSERT INTO LimitedDetails VALUES (?, ?)",
        (111, json.dumps({"background": "http://x/bg.jpg", "logo": "http://x/logo.jpg",
                          "logo2x": "http://x/logo2x.jpg", "icon": "http://x/i.png"})),
    )
    con.commit()
    con.close()
    urls = gog_lib.gog_image_urls("111", db_path=db)
    assert urls["hero"] == "http://x/bg.jpg"
    assert urls["logo"] == "http://x/logo2x.jpg"  # prefers 2x
    assert urls["icon"] == "http://x/i.png"


def test_gog_image_urls_missing(tmp_path):
    db = tmp_path / "g.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE LimitedDetails(productId INTEGER, images TEXT)")
    con.commit()
    con.close()
    assert gog_lib.gog_image_urls("999", db_path=db) == {}


def test_steamgriddb_url_is_encoded(monkeypatch):
    """Regression: game names with spaces/colons must be URL-encoded in the query."""
    import requests

    captured: dict = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": []}

    def _get(url, **kw):
        captured["url"] = url
        return _Resp()

    monkeypatch.setattr(requests, "get", _get)
    grid._steamgriddb_urls("SYNTHETIK: Legion Rising", "fake-key")
    url = captured["url"]
    assert "/search/autocomplete/" in url
    assert " " not in url          # space was encoded, not left raw
    assert "%3A" in url            # the colon was percent-encoded
