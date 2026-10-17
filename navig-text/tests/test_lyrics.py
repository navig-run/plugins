"""``navig text lyrics`` — fit, scaffold, demo sheets, word bank and rhymes.

Everything here reads files only, so the tests build small texts, word banks and bar maps
in a tmp dir. The bar map is the shape ``navig audio beat gen`` stores in a sidecar
(``measured.bars``): the arithmetic of the scaffold can be checked by hand from it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from navig_text.cli import app as standalone_app
from navig_text.commands.text import text_app
from navig_text.lyrics.bank import rhymes, search, tails, vocabulary
from navig_text.lyrics.fit import (
    count_syllables,
    detect_language,
    fit,
    natural_bpm,
    nearest_beat,
    parse,
    syllable_range,
)
from navig_text.lyrics.scaffold import ScaffoldError, plan, read_bar_map, render, sidecar_bpm
from navig_text.lyrics.sheet import SheetError, build_sheet, hook_lines, voice_table

runner = CliRunner()

# ── fit ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("line,lang,n", [
    ("Вышел из тумана — тут сосны считают шаги,", "ru", 14),
    ("Ладонь на стол — а линии как карта района,", "ru", 14),
    ("I walked out of the fog where the pines count steps,", "en", 12),
    ("Je sors du brouillard, les sapins comptent mes pas,", "fr", 12),
    ("На слово — никто. (стаб)", "ru", 5),
    ("Pouls ! Pouls ! / Ce qui coule à la place du sang !", "fr", 11),
])
def test_syllable_counts(line, lang, n):
    assert abs(count_syllables(line, lang) - n) <= 1


def test_language_detection():
    assert detect_language("Здесь никто не верит на слово") == "ru"
    assert detect_language("Ici personne te croit sur parole, c'est la preuve") == "fr"
    assert detect_language("Nobody here takes your word, it's the proof") == "en"


def test_ranges_follow_the_bar():
    assert syllable_range(76) == (10, 16)
    assert syllable_range(145, bars_per_line=2, fast=True) == (11, 20)
    assert natural_bpm(14) == pytest.approx(75.4, abs=0.5)


DRAFT = """# Title

## RU — «Тест»

### [ИНТРО]

Сэнсэй Монкс.

### [КУПЛЕТ 1 — 16 тактов]

Вышел из тумана — тут сосны считают шаги,
на коре чужие метки, красные, как ожоги.
Да.

### [ХУК — 8 тактов]

На слово — никто. (стаб)

## EN — "Test"

### [VERSE 1 — 16 bars]

I walked out of the fog where the pines count steps,

## Из Словаря

| a | b |
"""


def test_parse_the_draft_format(tmp_path):
    secs = parse(DRAFT)
    assert [(s.lang, s.kind) for s in secs] == [("ru", "other"), ("ru", "verse"), ("ru", "hook"), ("en", "verse")]
    assert len(secs[1].lines) == 3
    f = tmp_path / "d.md"
    f.write_text(DRAFT, encoding="utf-8")
    r = fit(f, bpm=76)
    assert r.out_count == 1  # «Да.» is a one-syllable verse line
    edits = [e for sf in r.sections for e in sf.edits(16) if sf.section.kind == "verse"]
    assert any("add 13 line(s)" in e for e in edits)


def test_a_plain_lyric_is_one_section_in_its_declared_language(tmp_path):
    f = tmp_path / "plain.md"
    f.write_text("# Travaill\n\nLanguage: French\n\nje travaille la nuit pour la ville\net la ville me le rend bien\n",
                 encoding="utf-8")
    r = fit(f)
    assert r.languages == ["fr"] and len(r.sections[0].section.lines) == 2 and r.bpm is None
    assert r.natural_bpm > 0


def test_the_nearest_beat_prefers_a_straight_match():
    lib = [("fiends-76", 76.0), ("veve-150", 150.0), ("squad-88", 88.0)]
    assert nearest_beat(75.4, lib)[0] == "fiends-76"  # not "veve at half-time"
    name, bpm, how = nearest_beat(176.0, lib)
    assert name == "squad-88" and how == "two lines per bar"


def test_fit_cli_single_file_and_report(tmp_path):
    f = tmp_path / "d.md"
    f.write_text(DRAFT, encoding="utf-8")
    r = runner.invoke(text_app, ["lyrics", "fit", str(f), "--bpm", "76"])
    assert r.exit_code == 0, r.output
    assert "1 verse line(s) off the bar" in r.output
    rep = tmp_path / "FIT.md"
    r = runner.invoke(text_app, ["lyrics", "fit", "--all", str(tmp_path), "--report", str(rep)])
    assert r.exit_code == 0, r.output
    body = rep.read_text(encoding="utf-8")
    assert "| [`d.md`](d.md) |" in body and "## Edits, file by file" in body


def test_the_standalone_command_has_lyrics_too(tmp_path):
    f = tmp_path / "d.md"
    f.write_text(DRAFT, encoding="utf-8")
    r = runner.invoke(standalone_app, ["lyrics", "fit", str(f), "--bpm", "76"])
    assert r.exit_code == 0, r.output


# ── scaffold ───────────────────────────────────────────────────────────────────

# A measured 88 bpm boom bap: 7-bar build, 17 full, a 7-bar drop, 17 full, 11-bar tail.
BARS = {
    "bpm": 88.0, "bar_s": 2.7273, "phase_s": 0.929, "bars": 59, "strip": "1" * 7 + "9" * 17 + "2" * 7 + "9" * 17 + "2" * 11,
    "blocks": [
        {"label": "build", "start_bar": 1, "end_bar": 7, "bars": 7},
        {"label": "full", "start_bar": 8, "end_bar": 24, "bars": 17},
        {"label": "drop", "start_bar": 25, "end_bar": 31, "bars": 7},
        {"label": "full", "start_bar": 32, "end_bar": 48, "bars": 17},
        {"label": "tail", "start_bar": 49, "end_bar": 59, "bars": 11},
    ],
}


def sidecar(tmp_path: Path, bars=BARS, *, bpm: float = 88.0) -> Path:
    beat = tmp_path / "beat.mp3"
    beat.write_bytes(b"")  # never read: the map comes from the sidecar
    (tmp_path / "beat.json").write_text(json.dumps({"target": {"bpm": bpm}, "measured": {"bars": bars}}),
                                        encoding="utf-8")
    return beat


def test_scaffold_maps_blocks_to_sections():
    slots = plan(BARS)
    assert [(s.kind, s.number, s.start_bar, s.lines) for s in slots] == [
        ("intro", 1, 1, 7), ("verse", 1, 8, 16), ("pre", 1, 24, 1), ("hook", 1, 25, 7),
        ("verse", 2, 32, 16), ("pre", 2, 48, 1), ("outro", 1, 49, 11),
    ]
    assert slots[1].start_s == pytest.approx(0.929 + 7 * 2.7273)


def test_scaffold_two_bars_a_line_leaves_a_hook_after_the_verse():
    bars = {"bar_s": 1.655, "phase_s": 0.14, "blocks": [
        {"label": "build", "start_bar": 1, "end_bar": 8, "bars": 8},
        {"label": "full", "start_bar": 9, "end_bar": 48, "bars": 40},
        {"label": "mid", "start_bar": 49, "end_bar": 58, "bars": 10},
    ]}
    slots = plan(bars, bars_per_line=2)
    assert [(s.kind, s.start_bar, s.lines, s.end_bar) for s in slots] == [
        ("intro", 1, 4, 8), ("verse", 9, 16, 40), ("hook", 41, 4, 48), ("hook", 49, 5, 58),
    ]


def test_a_rendered_scaffold_is_empty_to_fit_and_keeps_its_headings():
    text = render(plan(BARS), BARS, langs=["ru", "fr"], title="T", beat="beat.mp3", bpm=88)
    assert "### [КУПЛЕТ 1 — 16 тактов · 8–23 · 0:20.0]" in text
    assert "### [REFRAIN 1 — 7 mesures · 25–31 · 1:06.4]" in text
    assert "_ 1 · 9–14" in text
    # Every slot starts with "_", so an untouched scaffold has no line for fit to count.
    assert parse(text) == []


def test_scaffold_errors():
    with pytest.raises(ScaffoldError):
        plan({"blocks": []})
    with pytest.raises(ScaffoldError):
        render(plan(BARS), BARS, langs=["de"], title="T", beat="b", bpm=88)


def test_the_bar_map_is_read_from_a_sidecar_or_an_analyse_dump(tmp_path):
    beat = sidecar(tmp_path)
    assert read_bar_map(beat)["bars"] == 59 and sidecar_bpm(beat) == 88.0
    dump = tmp_path / "map.json"
    dump.write_text(json.dumps({"bars": BARS}), encoding="utf-8")
    assert read_bar_map(dump)["blocks"][2]["label"] == "drop"
    assert read_bar_map(tmp_path / "nothing.mp3") is None


def test_scaffold_cli_writes_once_and_refuses_to_overwrite(tmp_path):
    beat = sidecar(tmp_path)
    out = tmp_path / "draft.md"
    r = runner.invoke(text_app, ["lyrics", "scaffold", str(beat), "--lang", "en", "-o", str(out)])
    assert r.exit_code == 0, r.output
    assert "### [VERSE 2 — 16 bars · 32–47 · 1:25.5]" in out.read_text(encoding="utf-8")
    r = runner.invoke(text_app, ["lyrics", "scaffold", str(beat), "-o", str(out)])
    assert r.exit_code == 2 and "already exists" in r.output


def test_scaffold_cli_without_a_map_says_how_to_get_one(tmp_path, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_audio(name, *a, **kw):
        if name.startswith("navig_audio"):
            raise ImportError(name)
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_audio)
    beat = tmp_path / "bare.mp3"
    beat.write_bytes(b"")
    r = runner.invoke(text_app, ["lyrics", "scaffold", str(beat), "--bpm", "88"])
    assert r.exit_code == 2 and "No bar map" in r.output


# ── sheet ──────────────────────────────────────────────────────────────────────

TEXT = """# Night Round

## RU — «Ночной обход»

### [ПРЕ-ХУК]

Сквад!

### [ХУК — строка 1 хором]

Попасть — так же сложно, как уйти.
Лира крутит колесо — мы в пути. (лира)
Камера / считает одного,
один тебя считает, / другой поёт! / Пульс! / — вместо крови

## FR — « La Ronde »

### [REFRAIN]

Entrer, c'est aussi dur que partir.
La vielle tourne sa roue.

## Голоса хука

| Строки | RU / EN | FR | Зачем |
|---|---|---|---|
| 1 | `squad` | `squad` | хором |
| 2, 3, 4 | `monk` | `monk-fr` | рассказчик |
"""


def test_the_hook_skips_the_pre_hook_and_cleans_its_lines():
    assert hook_lines(TEXT, "ru") == [
        "Попасть — так же сложно, как уйти.", "Лира крутит колесо — мы в пути.", "Камера, считает одного",
        # a bar mark next to punctuation or a dash adds no comma
        "один тебя считает, другой поёт! Пульс! — вместо крови",
    ]
    assert hook_lines(TEXT, "fr")[1] == "La vielle tourne sa roue."
    assert hook_lines(TEXT, "en") == []


def test_the_voices_table_maps_lines_per_language():
    table = voice_table(TEXT)
    assert table["ru"] == {1: "squad", 2: "monk", 3: "monk", 4: "monk"} and table["en"] == table["ru"]
    assert table["fr"][2] == "monk-fr"


def test_build_sheet(tmp_path):
    sheet = build_sheet(TEXT, "fr", "C:/b/beat.mp3", start_bar=25, bpm=88)
    assert sheet["start_bar"] == 25 and sheet["lead_in_bars"] == 2  # a 2.7 s bar → two bars lead-in
    assert [ln["voice"] for ln in sheet["lines"]] == ["squad", "monk-fr"]
    assert build_sheet(TEXT, "ru", "b", start_block="drop")["start_block"] == "drop"
    with pytest.raises(SheetError, match="start_bar or start_block"):
        build_sheet(TEXT, "ru", "b")
    with pytest.raises(SheetError, match="no EN hook"):
        build_sheet(TEXT, "en", "b", start_bar=1)
    no_table = TEXT.split("## Голоса")[0]
    with pytest.raises(SheetError, match="no voice"):
        build_sheet(no_table, "ru", "b", start_bar=1)
    assert build_sheet(no_table, "ru", "b", start_bar=1, default_voice="monk")["lines"][0]["voice"] == "monk"


def test_sheet_cli_writes_yaml_the_demo_can_read(tmp_path):
    yaml = pytest.importorskip("yaml")
    beat = sidecar(tmp_path)
    src = tmp_path / "t.md"
    src.write_text(TEXT, encoding="utf-8")
    out = tmp_path / "demo.yaml"
    r = runner.invoke(text_app, ["lyrics", "sheet", str(src), "--beat", str(beat), "--lang", "ru",
                                 "--start-bar", "24", "-o", str(out)])
    assert r.exit_code == 0, r.output
    body = out.read_text(encoding="utf-8")
    assert body.startswith("# Guide-vocal demo — beat (ru) · GUIDE, NOT FOR RELEASE")
    data = yaml.safe_load(body)
    assert data["bpm"] == 88.0 and data["start_bar"] == 24 and len(data["lines"]) == 4


# ── word bank + rhymes ─────────────────────────────────────────────────────────


def bank(tmp_path: Path) -> Path:
    d = tmp_path / "dictionary"
    (d / "phrases").mkdir(parents=True)
    (d / "phrases" / "ru--klan.md").write_text("# | Клан чёрных капюшонов |\n\nЧёрные капюшоны.\n", encoding="utf-8")
    (d / "phrases" / "ru--ten.md").write_text("# Моя тень меня успокоит\n\nпро капюшон и тень\n", encoding="utf-8")
    (d / "README.md").write_text("# капюшон everywhere\n", encoding="utf-8")
    (d / "texts.md").write_text("района закона стена луна душа night light ronde monde rondes\n",
                                encoding="utf-8")
    return d


def test_search_puts_title_hits_first_and_skips_readmes(tmp_path):
    d = bank(tmp_path)
    hits = search([d], "капюшон")
    assert [h.path.name for h in hits] == ["ru--klan.md", "ru--ten.md"]
    assert hits[1].snippet == "про капюшон и тень"
    assert search([d], "  ") == []


@pytest.mark.parametrize("word,lang,expect", [
    ("района", "ru", ("она", "на")),
    ("стена", "ru", ("ена", "на")),
    ("night", "en", ("", "ight")),
    ("rondes", "fr", ("", "ond")),
])
def test_rhyme_tails(word, lang, expect):
    assert tails(word, lang) == expect


def test_rhymes_rank_strong_before_weak_and_respect_the_script(tmp_path):
    vocab = vocabulary([bank(tmp_path)])
    found = rhymes("района", vocab, lang="ru")
    assert found[0][:2] == ("закона", "strong")
    words = [w for w, _, _ in found]
    assert "стена" in words and "луна" in words and "душа" not in words and "night" not in words
    assert [w for w, *_ in rhymes("light", vocab, lang="en")] == ["night"]
    assert {w for w, *_ in rhymes("ronde", vocab, lang="fr")} == {"monde", "rondes"}


def test_words_and_rhyme_cli(tmp_path):
    d = bank(tmp_path)
    r = runner.invoke(text_app, ["lyrics", "words", "капюшон", "--bank", str(d), "--json"])
    assert r.exit_code == 0, r.output
    assert [e["title"] for e in json.loads(r.output)][0] == "Клан чёрных капюшонов"
    r = runner.invoke(text_app, ["lyrics", "rhyme", "района", "--in", str(d), "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)[0] == {"word": "закона", "match": "strong", "uses": 1}


# ── review fixes ───────────────────────────────────────────────────────────────


def test_a_malformed_bar_map_is_a_cli_error_not_a_traceback(tmp_path):
    bad = tmp_path / "map.json"
    bad.write_text("{ not json", encoding="utf-8")
    r = runner.invoke(text_app, ["lyrics", "scaffold", str(bad), "--bpm", "88"])
    assert r.exit_code == 2 and "Cannot scaffold this beat" in r.output
    f = tmp_path / "d.md"
    f.write_text(DRAFT, encoding="utf-8")
    r = runner.invoke(text_app, ["lyrics", "fit", str(f), "--beat", str(bad), "--bpm", "76"])
    assert r.exit_code == 2 and "bar map" in r.output


def test_a_relative_beat_path_is_anchored_where_typed_not_resolved(tmp_path, monkeypatch):
    src = tmp_path / "t.md"
    src.write_text(TEXT, encoding="utf-8")
    monkeypatch.setenv("NAVIG_INVOCATION_CWD", str(tmp_path))
    r = runner.invoke(text_app, ["lyrics", "sheet", str(src), "--beat", "media/beat.mp3", "--lang", "ru",
                                 "--start-bar", "1", "--bpm", "88"])
    assert r.exit_code == 0, r.output
    assert f"beat: {(tmp_path / 'media' / 'beat.mp3').as_posix()}" in r.output.replace("\n", "")


def test_the_whole_hook_goes_into_the_sheet():
    long_hook = "## EN\n\n### [HOOK]\n\n" + "\n".join(f"line {i}" for i in range(1, 11)) + "\n"
    sheet = build_sheet(long_hook, "en", "b", start_bar=1, default_voice="monk")
    assert len(sheet["lines"]) == 10
    assert len(build_sheet(long_hook, "en", "b", start_bar=1, default_voice="monk", max_lines=8)["lines"]) == 8
