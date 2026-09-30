"""`navig explore rename` — a Cyrillic, Notion-exported tree becomes clean Latin names.

The fixture is a small copy of what a real persona space looks like: Cyrillic folders, a
Notion title with ``Copy of … (2)``, a dictionary phrase with emoji and hashtags, a beat whose
JSON sidecar holds its own absolute path, a shotlist that points at audio three folders up,
a CSV with backslash paths, an audit log that must NOT be rewritten, and (on Windows) a
junction pointing outside the tree that must never be followed.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest
from typer.testing import CliRunner

from navig_explore.commands.explore import explore_app
from navig_explore.rename import (
    Entry,
    RenameMap,
    apply,
    clean_title,
    detect_language,
    detect_year,
    plan,
    slugify,
    undo,
    verify,
)

runner = CliRunner()


def build(t):
    (t / "songs/Биты/сгенерировано/kru-schema").mkdir(parents=True)
    (t / "songs/Эпоха-3/Треки").mkdir(parents=True)
    (t / "lyrics/Тексты").mkdir(parents=True)
    (t / "lyrics/Словарь/Фразы").mkdir(parents=True)
    (t / "videoclips/Эпоха-3/clip-01").mkdir(parents=True)
    (t / ".navig/state").mkdir(parents=True)
    (t / ".navig/logs").mkdir(parents=True)
    beat = t / "songs/Биты/сгенерировано/kru-schema/kru-boombap-88bpm-em-01.mp3"
    beat.write_bytes(b"ID3 beat bytes")
    beat.with_suffix(".json").write_text(json.dumps({"file": str(beat)}), encoding="utf-8")
    (t / "songs/Эпоха-3/Треки/b1ch.mp3").write_bytes(b"ID3 b1ch")
    (t / "songs/КАТАЛОГ.md").write_text(
        "[README](Биты/README.md) · `songs/Эпоха-3/Треки/b1ch.mp3`\n", encoding="utf-8")
    (t / "songs/Биты/README.md").write_text(
        "[стиль](../../lyrics/Тексты/Выбежал%20из%20леса.md)\n", encoding="utf-8")
    (t / "lyrics/Тексты/Выбежал из леса.md").write_text(
        "# Выбежал из леса\n\nLanguage: Russian\n\nя выбежал из леса\n", encoding="utf-8")
    (t / "lyrics/Тексты/Copy of Tracteur d espace (2).md").write_text(
        "# Tracteur\n\nCreated: April 16, 2023 5:25 PM\n\nje suis dans le tracteur\n", encoding="utf-8")
    (t / "lyrics/Словарь/Фразы/◾ it's a #death #trap(c).md").write_text("x\n", encoding="utf-8")
    (t / "videoclips/Эпоха-3/clip-01/clip-01.md").write_text(
        '---\naudio: "../../../songs/Эпоха-3/Треки/b1ch.mp3"\n---\n', encoding="utf-8")
    (t / ".navig/state/catalog.csv").write_text("id,file\n1,lyrics\\Тексты\\Выбежал из леса.md\n", encoding="utf-8")
    (t / ".navig/logs/old.csv").write_text("songs/Эпоха-3/Треки/b1ch.mp3\n", encoding="utf-8")


RMAP = RenameMap(
    names={"Биты": "beats", "сгенерировано": "generated", "Эпоха-3": "epoch-3", "Треки": "tracks",
           "Тексты": "texts", "Словарь": "dictionary", "Фразы": "phrases"},
    files={"КАТАЛОГ.md": "CATALOG.md"}, keep=["README.md", "*.json", "*.mp3"],
    words={"kru": "squad"}, tag_globs=["lyrics/**"], lang_globs={"lyrics/Словарь/Фразы/**": "en"},
    exclude=[".navig/**", "outside/**"], rewrite_exclude=[".navig/logs/**"],
)


def test_names():
    assert slugify("Выбежал из леса") == "vybezhal-iz-lesa"
    assert slugify("Cœur Steampunk — été") == "coeur-steampunk-ete"
    assert slugify("🐜тупа балдеж🐜") == "tupa-baldezh"
    assert clean_title("Copy of Tracteur d espace (2)") == "Tracteur d espace"
    assert clean_title("Page 1 0123456789abcdef0123456789abcdef") == "Page 1"
    assert clean_title("◾ Дзинь") == "Дзинь"
    assert detect_language("Language: French\n\nbonjour") == "fr"
    assert detect_language("я выбежал из леса") == "ru"
    assert detect_language("the forest and you") == "en"
    assert detect_year("Created: April 16, 2023 5:25 PM") == 2023
    assert detect_year("в 2000ом году") is None  # a year in a lyric is not a date
    assert detect_language("J’aimerai liberer ton esprit") == "fr"  # elision + French words
    assert detect_language("Hospital for Souls") == "en"  # Latin, no French signal
    assert detect_language("--- 12 ---") is None


def test_a_leading_underscore_folder_keeps_its_mark(tmp_path):
    (tmp_path / "_Прочее").mkdir()
    (tmp_path / "_Прочее" / "x.txt").write_text("x", encoding="utf-8")
    entries = plan(tmp_path, RenameMap())
    assert {e.src: e.dst for e in entries}["_Прочее"] == "_prochee"


def test_plan_apply_verify_undo(tmp_path):
    build(tmp_path)
    entries = plan(tmp_path, RMAP)
    got = {e.src: e.dst for e in entries if e.changes}
    assert got["lyrics/Тексты/Выбежал из леса.md"] == "lyrics/texts/ru--vybezhal-iz-lesa.md"
    assert got["lyrics/Тексты/Copy of Tracteur d espace (2).md"] == "lyrics/texts/fr-2023--tracteur-d-espace.md"
    assert got["lyrics/Словарь/Фразы/◾ it's a #death #trap(c).md"] == "lyrics/dictionary/phrases/en--its-a-death-trap-c.md"
    assert got["songs/Биты/сгенерировано/kru-schema/kru-boombap-88bpm-em-01.mp3"] == \
        "songs/beats/generated/squad-schema/squad-boombap-88bpm-em-01.mp3"
    assert got["songs/КАТАЛОГ.md"] == "songs/CATALOG.md"
    assert not any(e.src.startswith(".navig") for e in entries)

    logs = tmp_path / ".navig/logs/rename"
    res = apply(tmp_path, entries, logs, RMAP)
    assert res.errors == [] and res.moved == 9
    assert (tmp_path / "songs/CATALOG.md").read_text(encoding="utf-8").strip() == \
        "[README](beats/README.md) · `songs/epoch-3/tracks/b1ch.mp3`"
    assert "(../../lyrics/texts/ru--vybezhal-iz-lesa.md)" in (tmp_path / "songs/beats/README.md").read_text(encoding="utf-8")
    assert '"../../../songs/epoch-3/tracks/b1ch.mp3"' in (tmp_path / "videoclips/epoch-3/clip-01/clip-01.md").read_text(encoding="utf-8")
    assert "lyrics\\texts\\ru--vybezhal-iz-lesa.md" in (tmp_path / ".navig/state/catalog.csv").read_text(encoding="utf-8")
    side = json.loads((tmp_path / "songs/beats/generated/squad-schema/squad-boombap-88bpm-em-01.json").read_text(encoding="utf-8"))
    assert side["file"].endswith(os.path.join("songs", "beats", "generated", "squad-schema", "squad-boombap-88bpm-em-01.mp3"))
    # The audit log is history — never rewritten.
    assert (tmp_path / ".navig/logs/old.csv").read_text(encoding="utf-8") == "songs/Эпоха-3/Треки/b1ch.mp3\n"

    report = verify(tmp_path, logs, RMAP)
    assert report["ok"], report

    back = undo(tmp_path, logs)
    assert back["moved_back"] == 9
    assert (tmp_path / "lyrics/Тексты/Выбежал из леса.md").exists()
    assert (tmp_path / "songs/КАТАЛОГ.md").read_text(encoding="utf-8").startswith("[README](Биты/README.md)")
    assert not (tmp_path / "songs/beats").exists()


def test_a_move_under_a_new_prefix_is_rewritten_exactly_once(tmp_path):
    # songs/x.mp3 -> .media/songs/x.mp3: the new path *contains* the old one, so a second pass
    # over the text used to find `songs/x.mp3` again and write `.media/.media/songs/x.mp3`.
    (tmp_path / "songs").mkdir()
    (tmp_path / "songs/x.mp3").write_bytes(b"ID3 x")
    (tmp_path / "videoclips/c").mkdir(parents=True)
    (tmp_path / "videoclips/c/c.md").write_text('audio: "../../songs/x.mp3"\n', encoding="utf-8")
    (tmp_path / "CATALOG.md").write_text(
        f"`songs/x.mp3` · `{tmp_path.as_posix()}/songs/x.mp3`\n", encoding="utf-8")
    entries = [Entry("file", "songs/x.mp3", ".media/songs/x.mp3")]
    res = apply(tmp_path, entries, tmp_path / "logs", RenameMap())
    assert res.errors == []
    clip = (tmp_path / "videoclips/c/c.md").read_text(encoding="utf-8")
    cat = (tmp_path / "CATALOG.md").read_text(encoding="utf-8")
    assert clip == 'audio: "../../.media/songs/x.mp3"\n'
    assert cat == f"`.media/songs/x.mp3` · `{tmp_path.as_posix()}/.media/songs/x.mp3`\n"
    assert ".media/.media" not in clip + cat


def test_a_bare_file_relative_path_in_quotes_follows_its_file(tmp_path):
    # `image="footage/a.mp4"` names a file next to the shot list without ./ — it was never
    # rewritten, so a moved clip silently lost its footage.
    (tmp_path / "videoclips/c/footage").mkdir(parents=True)
    (tmp_path / "videoclips/c/footage/a.mp4").write_bytes(b"mp4")
    (tmp_path / "videoclips/c/c.md").write_text(
        '[shot: image="footage/a.mp4" secs=1]\nsee footage/a.mp4 in prose\n', encoding="utf-8")
    entries = [Entry("file", "videoclips/c/footage/a.mp4", ".media/videoclips/c/footage/a.mp4")]
    apply(tmp_path, entries, tmp_path / "logs", RenameMap())
    text = (tmp_path / "videoclips/c/c.md").read_text(encoding="utf-8")
    assert '[shot: image="../../.media/videoclips/c/footage/a.mp4" secs=1]' in text
    assert "see footage/a.mp4 in prose" in text  # unquoted prose is not a path — left alone


def test_collisions_get_a_suffix_not_an_overwrite(tmp_path):
    (tmp_path / "lyrics/Тексты").mkdir(parents=True)
    (tmp_path / "lyrics/Тексты/Сегодня.md").write_text("я\n", encoding="utf-8")
    (tmp_path / "lyrics/Тексты/Сегодня (1).md").write_text("я тоже\n", encoding="utf-8")
    entries = plan(tmp_path, RenameMap(names={"Тексты": "texts"}, tag_globs=["lyrics/**"]))
    dsts = sorted(e.dst for e in entries if e.kind == "file")
    assert dsts == ["lyrics/texts/ru--segodnya-2.md", "lyrics/texts/ru--segodnya.md"]


@pytest.mark.skipif(os.name != "nt", reason="junctions are a Windows thing")
def test_a_junction_is_never_followed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Секрет.txt").write_text("не трогать", encoding="utf-8")
    inside = tmp_path / "space"
    inside.mkdir()
    subprocess.run(["cmd", "/c", "mklink", "/J", str(inside / ".media"), str(outside)], capture_output=True, check=True)
    (inside / "Папка").mkdir()
    entries = plan(inside, RenameMap())
    assert all(".media" not in e.src for e in entries)
    assert (outside / "Секрет.txt").exists()


def test_cli_plan_then_apply_requires_yes(tmp_path):
    build(tmp_path)
    import yaml

    m = tmp_path / "map.yaml"
    m.write_text(yaml.safe_dump({
        "names": RMAP.names, "files": RMAP.files, "keep": RMAP.keep, "words": RMAP.words,
        "tag": {"globs": RMAP.tag_globs, "lang_globs": RMAP.lang_globs},
        "exclude": RMAP.exclude, "rewrite_exclude": RMAP.rewrite_exclude,
    }, allow_unicode=True), encoding="utf-8")
    r = runner.invoke(explore_app, ["rename", "plan", str(tmp_path), "--map", str(m), "--json"])
    assert r.exit_code == 0, r.output
    summary = json.loads(r.output)
    assert summary["files"] == 9 and summary["non_latin_left"] == []
    r = runner.invoke(explore_app, ["rename", "apply", str(tmp_path), "--plan", summary["plan"], "--map", str(m)])
    assert r.exit_code == 2 and (tmp_path / "songs/Биты").exists()
    logs = tmp_path / "logs"
    r = runner.invoke(explore_app, ["rename", "apply", str(tmp_path), "--plan", summary["plan"], "--map", str(m),
                                    "--log-dir", str(logs), "--yes"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(explore_app, ["rename", "verify", str(tmp_path), "--log-dir", str(logs), "--map", str(m), "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["ok"] is True


def test_a_case_only_folder_rename_really_changes_the_case(tmp_path):
    """On a case-insensitive disk the lowercase target "already exists" — it is the old folder.

    Found on a real run: brand/1gQwERagObo kept its capitals while every file inside moved.
    """
    (tmp_path / "Brand" / "IMG_01").mkdir(parents=True)
    (tmp_path / "Brand" / "IMG_01" / "Note.md").write_text("x", encoding="utf-8")
    logs = tmp_path / "logs"
    rmap = RenameMap(exclude=["logs/**"])
    entries = plan(tmp_path, rmap)
    res = apply(tmp_path, entries, logs, rmap)
    assert res.errors == []
    names = {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if "logs" not in p.parts}
    assert "brand/img-01/note.md" in names, names
    assert verify(tmp_path, logs, rmap)["ok"]
    undo(tmp_path, logs)
    back = {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if "logs" not in p.parts}
    assert "Brand/IMG_01/Note.md" in back, back


def test_the_map_file_is_never_rewritten(tmp_path):
    (tmp_path / "Тексты").mkdir()
    (tmp_path / "Тексты" / "a.md").write_text("x", encoding="utf-8")
    ops = tmp_path / "ops"
    ops.mkdir()
    m = ops / "map.yaml"
    m.write_text('names: {"Тексты": texts}\ntag: {globs: ["Тексты/**"]}\nexclude: ["ops/**", "logs/**"]\n',
                 encoding="utf-8")
    before = m.read_text(encoding="utf-8")
    rmap = RenameMap.load(m, tmp_path)
    apply(tmp_path, plan(tmp_path, rmap), tmp_path / "logs", rmap)
    assert m.read_text(encoding="utf-8") == before
