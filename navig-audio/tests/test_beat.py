"""`navig audio beat` — the beat has to be rappable, and the render has to be honest.

Rappable: sections are whole bars at the tempo asked for, there are no vocals, and a
house style can extend a built-in one without copying it. Honest: `--dry-run` sends
nothing, a render leaves its prompt and *measured* tempo next to the file, a take is
never overwritten, and an off-tempo render is flagged rather than filed as fine.

No live ElevenLabs calls: ``AudioGenerator`` is replaced at the core seam, and the
measurement is stubbed so the tests do not depend on ffmpeg decoding a fake mp3.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from navig_audio.beats import catalog as catalog_mod
from navig_audio.beats.catalog import append_index, make_record, tempo_matches
from navig_audio.beats.plan import (
    build_plan,
    key_name,
    key_short,
    next_index,
    parse_key,
    parse_structure,
    plan_seconds,
    prompt_text,
    slug,
    total_seconds,
)
from navig_audio.beats.styles import INSTRUMENTAL_NEGATIVE, StyleError, load_styles
from navig_audio.commands.audio import audio_app

runner = CliRunner()


# ── plan math ──────────────────────────────────────────────────────────────────

def test_sixty_bars_at_76_is_189_seconds():
    styles = load_styles()
    s = styles["boom-bap-dark"]
    plan = build_plan(s, bpm=76)
    assert plan_seconds(plan) == pytest.approx(189.47, abs=0.05)
    assert total_seconds(s.structure, 76) == pytest.approx(plan_seconds(plan), abs=0.01)
    names = [sec["section_name"] for sec in plan["sections"]]
    assert names[0].startswith("intro") and "4 bars" in names[0]
    assert names[3].startswith("verse (2)")
    # 16 bars at 76 bpm is 50.5 s — and it is exactly that, not "about a minute".
    assert plan["sections"][1]["duration_ms"] == 50526


def test_a_section_over_the_provider_cap_is_split_without_changing_the_total():
    styles = load_styles()
    plan = build_plan(styles["boom-bap-dark"], bpm=60, structure="verse:64")  # 256 s
    assert all(sec["duration_ms"] <= 120000 for sec in plan["sections"])
    assert sum(sec["duration_ms"] for sec in plan["sections"]) == 256000
    assert [sec["section_name"] for sec in plan["sections"]][0].startswith("verse 1")


def test_every_plan_is_instrumental_and_carries_tempo_and_key():
    plan = build_plan(load_styles()["trap-dark"], bpm=150, key="Gm")
    assert "150 bpm" in plan["positive_global_styles"]
    assert "G minor" in plan["positive_global_styles"]
    assert "instrumental" in plan["positive_global_styles"]
    for word in INSTRUMENTAL_NEGATIVE:
        assert word in plan["negative_global_styles"]
    assert all(sec["lines"] == [] for sec in plan["sections"])


@pytest.mark.parametrize("text,expected", [
    ("Em", ("E", "minor")), ("E minor", ("E", "minor")), ("e min", ("E", "minor")),
    ("Bb major", ("A#", "major")), ("F#", ("F#", "major")), ("C", ("C", "major")),
    ("Ab", ("G#", "major")),
])
def test_keys_are_read_the_way_producers_write_them(text, expected):
    assert parse_key(text) == expected


def test_a_key_that_is_not_a_key_is_refused():
    with pytest.raises(ValueError):
        parse_key("purple")
    assert key_name("em") == "E minor" and key_short("E minor") == "Em"


def test_structure_parsing_rejects_nonsense():
    assert parse_structure("intro:4, verse:16 ,hook:8") == [("intro", 4), ("verse", 16), ("hook", 8)]
    for bad in ("intro", "verse:sixteen", "hook:0", ""):
        with pytest.raises(ValueError):
            parse_structure(bad)


def test_slug_names_the_file_after_what_it_is():
    s = load_styles()["boom-bap-dark"]
    assert slug(s, bpm=76, key="Em", index=3) == "boom-bap-dark-76bpm-em-03"
    assert slug(s, bpm=142, key="F# minor", index=1) == "boom-bap-dark-142bpm-fsm-01"


def test_prompt_text_reads_as_one_brief():
    text = prompt_text(load_styles()["boom-bap-dark"], bpm=76, extra_negative=["flute"])
    assert "76 bpm, E minor, instrumental only, no vocals, structure: intro 4 bars - verse 16 bars" in text
    assert text.endswith("flute")


# ── styles ─────────────────────────────────────────────────────────────────────

def test_a_house_style_extends_a_builtin_and_later_files_win(tmp_path):
    house = tmp_path / "house.yaml"
    house.write_text(
        "styles:\n"
        "  my-boombap:\n"
        "    extends: boom-bap-dark\n"
        "    label: House boom bap\n"
        "    bpm: 88\n"
        "    positive_add: [toy piano, VHS hiss]\n"
        "    negative_add: [4k]\n"
        "    sections_add:\n"
        "      hook: [throat drone]\n"
        "  boom-bap-dark:\n"
        "    label: Overridden\n"
        "    bpm: 70\n",
        encoding="utf-8",
    )
    styles = load_styles([house])
    mine = styles["my-boombap"]
    assert mine.bpm == 88 and mine.key == "E minor"  # inherited key, own tempo
    assert mine.positive[:2] == load_styles()["boom-bap-dark"].positive[:2]
    assert "toy piano" in mine.positive and "4k" in mine.negative
    assert mine.sections["hook"][-1] == "throat drone" and mine.sections["intro"]  # inherited
    assert mine.source == "house.yaml"
    # The later definition replaces the built-in outright, and the child extended the
    # built-in as it was when the child was resolved (its own file's parent came later).
    assert styles["boom-bap-dark"].label == "Overridden" and styles["boom-bap-dark"].bpm == 70


def test_unknown_parent_and_missing_file_are_errors(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("styles:\n  x:\n    extends: nope\n", encoding="utf-8")
    with pytest.raises(StyleError):
        load_styles([bad])
    with pytest.raises(StyleError):
        load_styles([tmp_path / "missing.yaml"])


# ── catalogue ──────────────────────────────────────────────────────────────────

def test_tempo_matches_folds_half_and_double_time():
    assert tempo_matches(76, 76.5)
    assert tempo_matches(76, 152.0)  # double-time reading of the same beat
    assert tempo_matches(140, 70.0)  # halftime reading
    assert not tempo_matches(76, 82.0)
    assert not tempo_matches(76, 0.0)


def test_index_row_flags_an_off_tempo_render(tmp_path):
    rec = make_record(
        audio_path=tmp_path / "x-76bpm-em-01.mp3", style_id="x", prompt="p" * 200, plan={},
        model="music_v1", seed=None, target_bpm=76, target_key="E minor",
        measured={"bpm_felt": 82.0, "bpm_ok": False, "key": "E minor"},
    )
    index = append_index(tmp_path, rec)
    body = index.read_text(encoding="utf-8")
    assert body.startswith("# Beats")
    assert "76 → 82.0 ⚠" in body
    assert "…" in body  # the prompt is shortened in the table, kept whole in the sidecar


# ── the command ────────────────────────────────────────────────────────────────

def test_dry_run_sends_nothing(monkeypatch, tmp_path):
    import navig_generate.tools.audio_generation as ag

    class _Boom:
        def __init__(self, *a, **k):
            raise AssertionError("dry-run must not construct a generator")

    monkeypatch.setattr(ag, "AudioGenerator", _Boom)
    r = runner.invoke(audio_app, ["beat", "gen", "--style", "boom-bap-dark", "--bpm", "76",
                                  "-n", "3", "--out", str(tmp_path), "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "would render 3" in r.output and "189 s" in r.output
    assert not any(tmp_path.iterdir())


class _Aud:
    def __init__(self, audio: bytes | None, model: str = "music_v1"):
        self.audio = audio
        self.model = model
        self.generation_time = 1.5


def _patch_gen(monkeypatch, calls: list, *, audio: bytes | None = b"ID3fake"):
    import navig_generate.tools.audio_generation as ag

    class _Gen:
        def __init__(self, cfg):
            self.cfg = cfg

        async def generate(self, prompt, **kw):
            calls.append(kw)
            return _Aud(audio)

        async def close(self):
            return None

    class _Cfg:
        music_model = "music_v1"
        save_locally = True

        @classmethod
        def from_env(cls):
            return cls()

    monkeypatch.setattr(ag, "is_audio_generation_available", lambda: True)
    monkeypatch.setattr(ag, "AudioGenerationConfig", _Cfg)
    monkeypatch.setattr(ag, "AudioGenerator", _Gen)


def test_gen_writes_mp3_sidecar_and_index_and_never_overwrites(monkeypatch, tmp_path):
    calls: list = []
    _patch_gen(monkeypatch, calls)
    monkeypatch.setattr(catalog_mod, "measure",
                        lambda path, target_bpm=None: {"bpm_felt": 76.0, "bpm_ok": True, "key": "E minor",
                                                       "loudness_dbfs": -8.0})
    args = ["beat", "gen", "--style", "boom-bap-dark", "--bpm", "76", "-n", "2",
            "--out", str(tmp_path), "--seed", "7"]
    r = runner.invoke(audio_app, args)
    assert r.exit_code == 0, r.output
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["INDEX.md", "boom-bap-dark-76bpm-em-01.json", "boom-bap-dark-76bpm-em-01.mp3",
                     "boom-bap-dark-76bpm-em-02.json", "boom-bap-dark-76bpm-em-02.mp3"]
    # composition-plan mode, instrumental, seeds advance
    assert calls[0]["composition_plan"]["sections"][1]["duration_ms"] == 50526
    assert [c["seed"] for c in calls] == [7, 8]
    side = json.loads((tmp_path / "boom-bap-dark-76bpm-em-01.json").read_text(encoding="utf-8"))
    assert side["target"] == {"bpm": 76.0, "key": "E minor"} and side["measured"]["bpm_ok"] is True
    assert side["plan"]["positive_global_styles"][-1] == "instrumental"
    assert (tmp_path / "INDEX.md").read_text(encoding="utf-8").count("\n| 20") == 2
    # A second run continues the numbering instead of clobbering (or suffixing) the first takes.
    r = runner.invoke(audio_app, ["beat", "gen", "--style", "boom-bap-dark", "--bpm", "76", "-n", "1",
                                  "--out", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert (tmp_path / "boom-bap-dark-76bpm-em-03.mp3").exists()
    assert not list(tmp_path.glob("*take*"))
    # A different key in the same folder starts its own sequence.
    r = runner.invoke(audio_app, ["beat", "gen", "--style", "boom-bap-dark", "--bpm", "76", "--key", "Am",
                                  "-n", "1", "--out", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert (tmp_path / "boom-bap-dark-76bpm-am-01.mp3").exists()


def test_gen_prompt_mode_forces_instrumental_with_a_length(monkeypatch, tmp_path):
    calls: list = []
    _patch_gen(monkeypatch, calls)
    monkeypatch.setattr(catalog_mod, "measure", lambda path, target_bpm=None: {})
    r = runner.invoke(audio_app, ["beat", "gen", "--style", "boom-bap-dark", "--bpm", "76",
                                  "--out", str(tmp_path), "--prompt-mode", "--no-measure"])
    assert r.exit_code == 0, r.output
    assert calls[0]["force_instrumental"] is True
    assert calls[0]["duration_s"] == pytest.approx(189.47, abs=0.05)
    assert "composition_plan" not in calls[0]


def test_gen_with_no_audio_is_not_a_phantom_success(monkeypatch, tmp_path):
    calls: list = []
    _patch_gen(monkeypatch, calls, audio=None)
    r = runner.invoke(audio_app, ["beat", "gen", "--style", "boom-bap-dark", "--out", str(tmp_path)])
    assert r.exit_code == 1, r.output
    assert "No beat was rendered" in r.output
    assert not list(tmp_path.glob("*.mp3")) and not (tmp_path / "INDEX.md").exists()


def test_unknown_style_and_model_exit_2(tmp_path):
    r = runner.invoke(audio_app, ["beat", "gen", "--style", "nope", "--out", str(tmp_path), "--dry-run"])
    assert r.exit_code == 2 and "Unknown style" in r.output
    r = runner.invoke(audio_app, ["beat", "gen", "--style", "boom-bap-dark", "--model", "music_v9", "--dry-run"])
    assert r.exit_code == 2 and "Unknown model" in r.output


def test_plan_command_prints_sections_and_saves_json(tmp_path):
    out = tmp_path / "plan.json"
    r = runner.invoke(audio_app, ["beat", "plan", "--style", "phonk-halftime", "--structure", "intro:4,verse:16",
                                  "--out", str(out)])
    assert r.exit_code == 0, r.output
    plan = json.loads(out.read_text(encoding="utf-8"))
    assert len(plan["sections"]) == 2 and plan["sections"][0]["duration_ms"] == 6857  # 4 bars @ 140
    assert "34.3 s total" in r.output


def test_styles_command_lists_a_house_file(tmp_path):
    house = tmp_path / "h.yaml"
    house.write_text("styles:\n  mine:\n    extends: drill-uk\n    label: Mine\n", encoding="utf-8")
    r = runner.invoke(audio_app, ["beat", "styles", "--styles", str(house), "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["mine"]["bpm"] == 142 and data["mine"]["source"] == "h.yaml"


def test_next_index_reads_the_folder(tmp_path):
    s = load_styles()["boom-bap-dark"]
    assert next_index(tmp_path, s, bpm=76) == 1
    (tmp_path / "boom-bap-dark-76bpm-em-01.mp3").write_bytes(b"x")
    (tmp_path / "boom-bap-dark-76bpm-em-07.mp3").write_bytes(b"x")
    (tmp_path / "boom-bap-dark-76bpm-am-09.mp3").write_bytes(b"x")  # another key, another sequence
    assert next_index(tmp_path, s, bpm=76) == 8
    assert next_index(tmp_path, s, bpm=76, key="Am") == 10
