"""navig audio songs — the song card, the facts read from a folder, the generated catalog."""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from navig_audio.commands.audio import audio_app  # noqa: E402
from navig_audio.songs import card as card_mod  # noqa: E402
from navig_audio.songs.card import FACTS_BEGIN, SongError, inspect_folder, read_card, scan, write_card  # noqa: E402
from navig_audio.songs.catalog import BEGIN, END, problems, render_catalog, write_catalog, write_csv  # noqa: E402

runner = CliRunner()


def _song(root: Path, name: str, *, master: bool = True) -> Path:
    d = root / name
    (d / "takes").mkdir(parents=True)
    (d / "stems").mkdir()
    (d / "ru--odin.md").write_text("# Один\n", encoding="utf-8")
    (d / "ru--odin--transcript.md").write_text("…", encoding="utf-8")
    if master:
        (d / "Сенсей Монкс - Один.mp3").write_bytes(b"x" * 10)
        (d / "Один.master.wav").write_bytes(b"x" * 5)
    (d / "takes" / "t1.mp3").write_bytes(b"x")
    (d / "takes" / "t2.wav").write_bytes(b"x")
    (d / "stems" / "vocals.mp3").write_bytes(b"x")
    (d / "notes.txt").write_text("not a lyric", encoding="utf-8")
    return d


def test_facts_read_the_folder(tmp_path):
    d = _song(tmp_path, "05-odin")
    f = inspect_folder(d)
    assert f.lyrics == ["ru--odin.md"] and f.transcripts == ["ru--odin--transcript.md"]
    assert f.master == "Один.master.wav"  # 'master' in the name beats the bigger mp3
    assert f.versions == ["Сенсей Монкс - Один.mp3"]
    assert f.takes == 2 and f.stems == ["vocals"]
    assert inspect_folder(d, master="Сенсей Монкс - Один.mp3").master == "Сенсей Монкс - Один.mp3"


def test_card_round_trip_keeps_owner_keys_and_text(tmp_path):
    d = _song(tmp_path, "05-odin")
    write_card(d, {"id": "snm-019", "title": "Один", "status": "released", "release": "Холодец", "track": 5})
    text = (d / "README.md").read_text(encoding="utf-8")
    assert text.startswith("---\ntype: song\nid: snm-019\ntitle: Один\nstatus: released")
    # the owner adds a key and a note; a refresh keeps both and rewrites only the facts block
    text = text.replace("track: 5", "track: 5\nmood: ночь") + "\nНаписано в бункере.\n"
    (d / "README.md").write_text(text, encoding="utf-8")
    (d / "takes" / "t3.mp3").write_bytes(b"x")
    write_card(d)
    s = read_card(d)
    assert s.meta["mood"] == "ночь" and s.meta["id"] == "snm-019"
    body = (d / "README.md").read_text(encoding="utf-8")
    assert "Написано в бункере." in body and "3 in `takes/`" in body
    assert body.count(FACTS_BEGIN) == 1


def test_a_plain_readme_becomes_a_card_without_losing_its_text(tmp_path):
    d = _song(tmp_path, "x")
    (d / "README.md").write_text("# Одна песня\n\nИстория записи.\n", encoding="utf-8")
    write_card(d, {"status": "recorded"})
    s = read_card(d)
    assert s.title == "Одна песня"  # a plain README's heading names the song
    body = (d / "README.md").read_text(encoding="utf-8")
    assert "# Одна песня" in body and "История записи." in body


def test_unknown_status_and_foreign_card_are_refused(tmp_path):
    d = _song(tmp_path, "x")
    with pytest.raises(SongError):
        write_card(d, {"status": "finished-ish"})
    (d / "README.md").write_text("---\ntype: album\n---\n# A\n", encoding="utf-8")
    with pytest.raises(SongError):
        write_card(d)


def test_scan_skips_non_cards_and_hidden_folders(tmp_path):
    write_card(_song(tmp_path, "a"), {"status": "released"})
    write_card(_song(tmp_path / ".trash", "b"), {"status": "released"})
    (tmp_path / "album").mkdir()
    (tmp_path / "album" / "README.md").write_text("# Album\n", encoding="utf-8")
    assert [s.folder.name for s in scan(tmp_path)] == ["a"]


def test_catalog_groups_by_release_and_keeps_the_hand_written_part(tmp_path):
    root = tmp_path / "songs"
    write_card(_song(root / "albums" / "k", "02-two"), {"title": "Two", "status": "released", "release": "K",
                                                         "year": 2016, "track": 2})
    write_card(_song(root / "albums" / "k", "01-one"), {"title": "One", "status": "released", "release": "K",
                                                         "year": 2016, "track": 1})
    write_card(_song(root / "recorded", "loose", master=False), {"title": "Loose", "status": "recorded"})
    songs = scan(root)
    md = render_catalog(songs, root)
    assert md.index("One") < md.index("Two") < md.index("Not released — recorded")
    assert "(albums/k/01-one/README.md)" in md
    cat = root / "CATALOG.md"
    cat.write_text("# Каталог\n\nМои заметки.\n", encoding="utf-8")
    write_catalog(songs, cat)
    write_catalog(songs, cat)  # idempotent: one block, notes kept
    text = cat.read_text(encoding="utf-8")
    assert text.count(BEGIN) == 1 and text.count(END) == 1 and "Мои заметки." in text
    rows = list(csv.DictReader(write_csv(songs, tmp_path / "songs.csv", root).open(encoding="utf-8-sig")))
    assert {r["title"] for r in rows} == {"One", "Two", "Loose"} and rows[0]["folder"].startswith("albums/")
    assert any("recorded but no audio" in p for p in problems(songs))


def test_duplicate_ids_are_reported(tmp_path):
    write_card(_song(tmp_path, "a"), {"id": "snm-1", "status": "released"})
    write_card(_song(tmp_path, "b"), {"id": "snm-1", "status": "released"})
    assert any("snm-1 is used by 2" in p for p in problems(scan(tmp_path)))


def test_cli_card_catalog_and_check(tmp_path):
    d = _song(tmp_path, "01-x")
    r = runner.invoke(audio_app, ["songs", "card", str(d), "--title", "X", "--status", "released", "--id", "snm-9"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(audio_app, ["songs", "catalog", str(tmp_path), "--md", str(tmp_path / "CATALOG.md"),
                                  "--csv", str(tmp_path / "songs.csv")])
    assert r.exit_code == 0, r.output
    assert (tmp_path / "CATALOG.md").is_file() and (tmp_path / "songs.csv").is_file()
    assert runner.invoke(audio_app, ["songs", "check", str(tmp_path)]).exit_code == 0
    bad = runner.invoke(audio_app, ["songs", "card", str(d), "--status", "nope"])
    assert bad.exit_code == 2


def test_lyric_pattern():
    ok = ["ru--odin.md", "fr-2019--void.md", "xx--ty-vse-eshche-tut--transcript.md", "ru/en--mix.md"]
    assert all(card_mod.LYRIC.match(n) for n in ok[:3])
    assert not card_mod.LYRIC.match("README.md") and not card_mod.LYRIC.match("notes.md")


def test_years_column_and_csv(tmp_path):
    from navig_audio.songs.catalog import _year, years

    root = tmp_path / "songs"
    write_card(_song(root / "albums" / "b", "01-a"), {"title": "A", "status": "recorded", "release": "Бункер",
                                                       "written": "2016-07-27", "recorded": "2017-07-25"})
    write_card(_song(root / "albums" / "b", "02-b"), {"title": "B", "status": "recorded", "release": "Бункер",
                                                       "recorded": "≤2019"})
    songs = scan(root)
    a, b = songs
    assert years(a) == "✍ 2016-07-27 · 🎙 2017-07-25" and _year(a) == 2017
    assert _year(b) == 2019  # '≤2019' still sorts as 2019
    md = render_catalog(songs, root)
    assert "### Бункер (2017–2019)" in md and "| Years |" in md and "🎙 ≤2019" in md
    rows = list(csv.DictReader(write_csv(songs, tmp_path / "s.csv", root).open(encoding="utf-8-sig")))
    assert rows[0]["written"] == "2016-07-27" and rows[0]["recorded"] == "2017-07-25"
    text = (root / "albums" / "b" / "01-a" / "README.md").read_text(encoding="utf-8")
    assert text.index("written:") < text.index("recorded:") < text.index("lang:") if "lang:" in text else True


def test_release_header_does_not_repeat_its_year(tmp_path):
    from navig_audio.songs.catalog import span

    root = tmp_path / "songs"
    write_card(_song(root, "s"), {"title": "S", "status": "released", "release": "Сингл 2019", "year": 2019})
    assert span(scan(root), "Сингл 2019") == ""
    assert span(scan(root), "Альбом") == " (2019)"
