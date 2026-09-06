"""Steam unification — add non-Steam games, dedupe, and the safety guards."""

from pathlib import Path

from navig_games.engine.library.models import InstalledGame
from navig_games.engine.steam import shortcuts as sc


def _patch(monkeypatch, tmp_path, running=False):
    (tmp_path / "userdata" / "123" / "config").mkdir(parents=True)
    monkeypatch.setattr(sc, "steam_path", lambda: tmp_path)
    monkeypatch.setattr(sc, "active_user_id3", lambda steam=None: "123")
    monkeypatch.setattr(sc, "is_steam_running", lambda: running)
    monkeypatch.setattr(
        sc, "shortcuts_path",
        lambda steam, id3: tmp_path / "userdata" / id3 / "config" / "shortcuts.vdf",
    )
    return tmp_path / "userdata" / "123" / "config" / "shortcuts.vdf"


def test_add_and_dedupe(tmp_path, monkeypatch):
    path = _patch(monkeypatch, tmp_path)
    g = InstalledGame(store="gog", title="Cool", launch_exe="C:/g/cool.exe")

    r1 = sc.add_games([g])
    assert r1["ok"] and r1["added"] == ["Cool"] and path.exists()

    r2 = sc.add_games([g])  # idempotent — must not duplicate
    assert r2["added"] == [] and r2["skipped_present"] == ["Cool"]
    assert len(sc.load_shortcuts(path)) == 1


def test_entry_structure(tmp_path, monkeypatch):
    path = _patch(monkeypatch, tmp_path)
    sc.add_games([InstalledGame(store="gog", title="Cool", launch_exe="C:/g/cool.exe")])
    entry = sc.load_shortcuts(path)["0"]
    assert entry["AppName"] == "Cool"
    assert entry["Exe"] == '"C:/g/cool.exe"'  # quoted, source separators preserved
    assert entry["StartDir"] == f'"{Path("C:/g/cool.exe").parent}"'  # OS-normalized parent
    assert entry["tags"]["0"] == "navig"
    assert entry["AllowOverlay"] == 1


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    path = _patch(monkeypatch, tmp_path)
    r = sc.add_games([InstalledGame(store="gog", title="Cool", launch_exe="C:/g/cool.exe")],
                     dry_run=True)
    assert r["added"] == ["Cool"] and not path.exists()


def test_refuses_when_steam_running(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path, running=True)
    r = sc.add_games([InstalledGame(store="gog", title="X", launch_exe="C:/x.exe")])
    assert not r["ok"] and "running" in r["error"]


def test_skips_unlaunchable(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    r = sc.add_games([InstalledGame(store="gog", title="NoExe")])  # no launch_exe
    assert r["skipped_unlaunchable"] == ["NoExe"] and r["added"] == []


def test_backup_made_on_second_write(tmp_path, monkeypatch):
    path = _patch(monkeypatch, tmp_path)
    sc.add_games([InstalledGame(store="gog", title="A", launch_exe="C:/a.exe")])
    sc.add_games([InstalledGame(store="gog", title="B", launch_exe="C:/b.exe")])
    backups = list(path.parent.glob("shortcuts.vdf.bak-*"))
    assert backups, "expected a backup before the second write"
    assert len(sc.load_shortcuts(path)) == 2
