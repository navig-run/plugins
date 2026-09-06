"""Library scanners — parse each launcher's on-disk catalog (fixtures)."""

import json
import sqlite3

from navig_games.engine.library import epic as epic_lib
from navig_games.engine.library import gog as gog_lib
from navig_games.engine.library import steam as steam_lib


def _lib_vdf(path: str) -> str:
    esc = path.replace("\\", "\\\\")
    return '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n}' % esc


def test_epic_parse(tmp_path):
    item = {"DisplayName": "Cool Game", "InstallLocation": str(tmp_path / "Cool"),
            "LaunchExecutable": "Cool.exe", "AppName": "cool123"}
    (tmp_path / "a.item").write_text(json.dumps(item), encoding="utf-8")
    games = epic_lib.installed_epic_games(manifests_dir=tmp_path)
    assert len(games) == 1
    g = games[0]
    assert g.store == "epic" and g.title == "Cool Game"
    assert g.launch_exe.endswith("Cool.exe") and g.launchable


def test_epic_missing_dir_is_empty(tmp_path):
    assert epic_lib.installed_epic_games(manifests_dir=tmp_path / "nope") == []


def test_steam_parse(tmp_path):
    apps = tmp_path / "steamapps"
    apps.mkdir()
    (apps / "libraryfolders.vdf").write_text(_lib_vdf(str(tmp_path)), encoding="utf-8")
    (apps / "appmanifest_570.acf").write_text(
        '"AppState"\n{\n\t"appid"\t"570"\n\t"name"\t"Dota 2"\n\t"installdir"\t"dota2"\n}',
        encoding="utf-8",
    )
    games = steam_lib.installed_steam_games(steam=tmp_path)
    assert [g.title for g in games] == ["Dota 2"]
    assert games[0].app_id == "570"


def test_steam_skips_redistributables(tmp_path):
    apps = tmp_path / "steamapps"
    apps.mkdir()
    (apps / "libraryfolders.vdf").write_text(_lib_vdf(str(tmp_path)), encoding="utf-8")
    (apps / "appmanifest_228980.acf").write_text(
        '"AppState"\n{\n\t"appid"\t"228980"\n\t"name"\t"Steamworks Common Redistributables"'
        '\n\t"installdir"\t"x"\n}',
        encoding="utf-8",
    )
    assert steam_lib.installed_steam_games(steam=tmp_path) == []


def test_gog_parse(tmp_path):
    db = tmp_path / "galaxy.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE InstalledBaseProducts(productId INTEGER, installationPath TEXT);
        CREATE TABLE LimitedDetails(productId INTEGER, title TEXT);
        CREATE TABLE PlayTasks(id INTEGER, gameReleaseKey TEXT, isPrimary INTEGER);
        CREATE TABLE PlayTaskLaunchParameters(playTaskId INTEGER, executablePath TEXT, commandLineArgs TEXT);
        INSERT INTO InstalledBaseProducts VALUES (111, 'C:/Games/Cool');
        INSERT INTO LimitedDetails VALUES (111, 'Cool GOG Game');
        INSERT INTO PlayTasks VALUES (5, 'gog_111', 1);
        INSERT INTO PlayTaskLaunchParameters VALUES (5, 'C:/Games/Cool/cool.exe', '');
        """
    )
    con.commit()
    con.close()
    games = gog_lib.installed_gog_games(db_path=db)
    assert len(games) == 1
    g = games[0]
    assert g.title == "Cool GOG Game"
    assert g.launch_exe == "C:/Games/Cool/cool.exe"
    assert g.launchable and g.app_id == "111"
