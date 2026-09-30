"""Bar maps, sound-effect layering and guide-vocal demos (lyric fitting: navig-text).

The bar-map fixture is synthesised so the right answer is arithmetic: a 120 bpm click track
(bar = 2 s) that is quiet for 8 bars, loud for 16, drops out for 4, loud for 8 and fades to
nothing — the blocks must come back in that order at those bars. Everything that would call
ElevenLabs is either a dry run or fed from a pre-seeded cache, so no test touches the network.
"""

from __future__ import annotations

import json
import shutil
import struct
import wave
from pathlib import Path

import pytest
from typer.testing import CliRunner

np = pytest.importorskip("numpy")

from navig_audio.beats.bars import BarMap, Block, bar_map, classify  # noqa: E402
from navig_audio.beats.demo import (  # noqa: E402
    GUIDE_TAG,
    DemoError,
    Voice,
    build_graph as demo_graph,
    load_voices,
    plan_demo,
    tts_cache_name,
)
from navig_audio.beats.layer import (  # noqa: E402
    LayerError,
    Master,
    Occurrence,
    build_graph as layer_graph,
    plan_layers,
    sfx_cache_name,
)
from navig_audio.commands.audio import audio_app  # noqa: E402

runner = CliRunner()
SR = 22050
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def arranged_track(path: Path, *, bpm: float = 120.0, layout=((8, 0.08), (16, 1.0), (4, 0.06), (8, 1.0), (4, 0.004))) -> Path:
    """Clicks on every beat (accented on the one) + a tone, block by block at the given gains."""
    bar_s = 240.0 / bpm
    total_bars = sum(n for n, _ in layout)
    audio = np.zeros(int(total_bars * bar_s * SR), dtype="float32")
    click = np.exp(-np.linspace(0, 9, int(0.02 * SR))).astype("float32")
    rng = np.random.default_rng(3)
    bar = 0
    for n, gain in layout:
        for b in range(bar, bar + n):
            for beat in range(4):
                start = int((b * bar_s + beat * bar_s / 4) * SR)
                amp = gain * (1.0 if beat == 0 else 0.6)
                seg = rng.normal(0, 1, click.size).astype("float32") * click * amp
                audio[start:start + click.size] += seg[: max(0, min(click.size, audio.size - start))]
            a, z = int(b * bar_s * SR), int((b + 1) * bar_s * SR)
            t = np.arange(z - a) / SR
            audio[a:z] += (0.2 * gain * np.sin(2 * np.pi * 110 * t)).astype("float32")
        bar += n
    audio = np.clip(audio, -1, 1)
    with wave.open(str(path), "wb") as h:
        h.setnchannels(1)
        h.setsampwidth(2)
        h.setframerate(SR)
        h.writeframes(b"".join(struct.pack("<h", int(s * 32000)) for s in audio))
    return path


# ── bars ───────────────────────────────────────────────────────────────────────

def test_classify_labels_build_full_drop_tail_and_folds_one_bar_blips():
    digits = [2, 2, 3, 8, 3, 3, 9, 9, 9, 9, 1, 1, 9, 9, 9, 7, 9, 0, 0]
    #          build …  ↑ a one-bar loud blip inside the build is folded, not a section
    runs = classify(digits)
    assert [r[0] for r in runs] == ["build", "full", "drop", "full", "tail"]
    assert runs[0] == ("build", 0, 5)
    assert runs[2] == ("drop", 10, 11)


def test_a_one_bar_silence_between_full_bars_is_kept():
    runs = classify([9, 9, 9, 9, 1, 9, 9, 9])
    assert [r[0] for r in runs] == ["full", "drop", "full"]


@needs_ffmpeg
def test_bar_map_finds_the_arrangement_it_was_built_with(tmp_path):
    bm = bar_map(arranged_track(tmp_path / "a.wav"), 120.0)
    assert bm.bar_s == pytest.approx(2.0)
    labels = [b.label for b in bm.blocks]
    assert labels == ["build", "full", "drop", "full", "tail"]
    starts = [b.start_bar for b in bm.blocks]
    for got, want in zip(starts, [1, 9, 25, 29, 37]):
        assert abs(got - want) <= 1
    assert bm.block("full:2").start_bar == starts[3]
    with pytest.raises(ValueError):
        bm.block("drop:2")
    md = bm.to_markdown()
    assert "| full |" in md and "bar   1 @" in md


def test_bar_time_and_to_dict():
    bm = BarMap(bpm=76, bar_s=240 / 76, phase_s=0.79, duration_s=190.0, levels_db=[-30, -8, -7],
                blocks=[Block("build", 1, 1, 0.79, 3.95, 0), Block("full", 2, 3, 3.95, 10.26, 9)])
    assert bm.bar_time(25) == pytest.approx(0.79 + 24 * 240 / 76)
    d = bm.to_dict()
    assert d["bars"] == 3 and d["blocks"][1]["bars"] == 2 and len(d["strip"]) == 3


# ── fit (moved to navig-text; only the alias is tested here) ───────────────────

def test_beat_fit_is_an_alias_of_text_lyrics_fit(tmp_path):
    pytest.importorskip("navig_text")
    f = tmp_path / "d.md"
    f.write_text("# T\n\n## RU\n\n### [КУПЛЕТ 1]\n\nВышел из тумана — тут сосны считают шаги,\nДа.\n",
                 encoding="utf-8")
    r = runner.invoke(audio_app, ["beat", "fit", str(f), "--bpm", "76"])
    assert r.exit_code == 0, r.output
    assert "1 verse line(s) off the bar" in r.output


# ── layer ──────────────────────────────────────────────────────────────────────

def _bm(bpm=120.0):
    return BarMap(bpm=bpm, bar_s=240 / bpm, phase_s=0.5, duration_s=80.0, levels_db=[0.0] * 40,
                  blocks=[Block("build", 1, 8, 0.5, 16.5, 1), Block("full", 9, 24, 16.5, 48.5, 9),
                          Block("drop", 25, 28, 48.5, 56.5, 0), Block("tail", 29, 40, 56.5, 80.5, 0)])


def test_plan_layers_resolves_bars_blocks_repeats_and_cache(tmp_path):
    (tmp_path / "bell.wav").write_bytes(b"x")
    sheet = {"layers": [
        {"name": "bell", "file": "bell.wav", "at_bar": 1, "repeat": 3, "every_bars": 8, "gain_db": -10},
        {"name": "drop-fx", "prompt": "a whoosh", "duration": 3, "block": "drop", "fade_in": 0.5},
        {"name": "again", "prompt": "a whoosh", "duration": 3, "block": "tail", "edge": "start", "offset_bars": 2},
    ]}
    lp = plan_layers(sheet, bar_map=_bm(), cache_dir=tmp_path / ".sfx", base_dir=tmp_path)
    starts = [round(o.start_s, 2) for o in lp.occurrences]
    assert starts == [0.5, 16.5, 32.5, 48.5, 60.5]
    assert len(lp.to_generate) == 1  # the same prompt twice is generated once
    assert lp.to_generate[0][2].name == sfx_cache_name("a whoosh", 3.0)


def test_plan_layers_errors(tmp_path):
    with pytest.raises(LayerError):
        plan_layers({"layers": []}, bar_map=_bm(), cache_dir=tmp_path, base_dir=tmp_path)
    with pytest.raises(LayerError):
        plan_layers({"layers": [{"name": "x", "file": "missing.wav", "at_bar": 1}]},
                    bar_map=_bm(), cache_dir=tmp_path, base_dir=tmp_path)
    with pytest.raises(LayerError):
        plan_layers({"layers": [{"name": "x", "prompt": "p"}]}, bar_map=_bm(), cache_dir=tmp_path, base_dir=tmp_path)


def test_layer_graph_shapes_each_occurrence_and_the_master():
    occ = [Occurrence("bell", Path("b.wav"), 1.25, length_s=4.0, gain_db=-10, fade_in=0.5, fade_out=1.5),
           Occurrence("wind", Path("w.wav"), 60.0, gain_db=-16)]
    g = layer_graph(80.0, occ, Master(fade_in=1.0, fade_out=3.0, lufs=-14))
    assert "[1:a]" in g and "atrim=0:4.000" in g and "afade=t=out:st=2.500:d=1.500" in g
    assert "volume=-10.0dB" in g and "adelay=1250|1250" in g and "adelay=60000|60000" in g
    assert "amix=inputs=3:normalize=0" in g and "afade=t=out:st=77.000:d=3.000" in g
    assert g.endswith("loudnorm=I=-14:TP=-1.0:LRA=11[out]")


# ── demo ───────────────────────────────────────────────────────────────────────

VOICES = {"voices": {
    "monk": {"voice_id": "v1", "pitch": -3, "formant": "keep", "fx": ["room"]},
    "chip": {"voice_id": "v2", "pitch": 9, "formant": "shift", "fx": ["robot"], "gain_db": -2},
}}


def test_voice_chain_pitch_formant_and_effects():
    vs = load_voices(VOICES)
    c = vs["chip"].chain()
    assert c[0].startswith("rubberband=pitch=1.68179:formant=shifted")
    assert any("flanger" in x for x in c) and c[-1] == "volume=-2.0dB"
    assert vs["monk"].chain()[0].endswith("formant=preserved")
    with pytest.raises(DemoError):
        Voice("x", "id", fx=["nope"]).chain()
    with pytest.raises(DemoError):
        load_voices({"voices": {"a": {"pitch": 2}}})


def test_plan_demo_places_lines_on_bars_and_drops_what_falls_outside(tmp_path):
    vs = load_voices(VOICES)
    sheet = {"start_block": "full", "lead_in_bars": 2, "length_s": 10,
             "lines": [{"text": "one", "voice": "monk"}, {"text": "two", "voice": "chip"},
                       {"text": "later", "voice": "monk", "bar": 6}]}
    plan = plan_demo(sheet, vs, _bm(), tmp_path)
    assert plan.start_s == pytest.approx(_bm().bar_time(7))
    assert [round(p.at_s, 2) for p in plan.placed] == [4.0, 6.0]  # bar 6 would start at 16 s > 10 s
    assert plan.placed[0].clip.name == tts_cache_name(vs["monk"], "one")
    g = demo_graph(plan, vs)
    assert "sidechaincompress" in g and "asplit=2[vx][sc]" in g and "adelay=4000|4000" in g
    with pytest.raises(DemoError):
        plan_demo({"start_bar": 1, "lines": [{"text": "x", "voice": "ghost"}]}, vs, _bm(), tmp_path)


@needs_ffmpeg
def test_demo_and_layer_cli_dry_runs_never_call_the_generator(tmp_path, monkeypatch):
    import yaml

    import navig_generate.tools.audio_generation as ag

    class _Boom:
        def __init__(self, *a, **k):
            raise AssertionError("a dry run must not construct a generator")

    monkeypatch.setattr(ag, "AudioGenerator", _Boom)
    beat = arranged_track(tmp_path / "beat.wav")
    (tmp_path / "voices.yaml").write_text(yaml.safe_dump(VOICES), encoding="utf-8")
    (tmp_path / "demo.yaml").write_text(yaml.safe_dump({
        "language": "en", "start_block": "full", "lines": [{"text": "one", "voice": "monk"}]}), encoding="utf-8")
    r = runner.invoke(audio_app, ["beat", "demo", str(beat), "--sheet", str(tmp_path / "demo.yaml"),
                                  "--voices", str(tmp_path / "voices.yaml"), "--bpm", "120", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "1 line(s)" in r.output and not list(tmp_path.glob("*-demo-*.mp3"))
    (tmp_path / "layers.yaml").write_text(yaml.safe_dump({"layers": [
        {"name": "fx", "prompt": "a whoosh", "duration": 2, "block": "drop"}]}), encoding="utf-8")
    r = runner.invoke(audio_app, ["beat", "layer", str(beat), "--sheet", str(tmp_path / "layers.yaml"),
                                  "--bpm", "120", "--dry-run", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["placements"][0]["name"] == "fx" and data["to_generate"][0]["prompt"] == "a whoosh"


@needs_ffmpeg
def test_demo_renders_a_tagged_30s_file_from_a_seeded_cache(tmp_path):
    import subprocess

    import yaml

    vs = load_voices(VOICES)
    beat = arranged_track(tmp_path / "beat.wav")
    cache = tmp_path / ".tts"
    cache.mkdir()
    for text, voice in (("one", "monk"), ("two", "chip")):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=330:d=1.5",
                        str(cache / tts_cache_name(vs[voice], text))], check=True)
    (tmp_path / "voices.yaml").write_text(yaml.safe_dump(VOICES), encoding="utf-8")
    (tmp_path / "demo.yaml").write_text(yaml.safe_dump({
        "language": "ru", "start_block": "full", "length_s": 12,
        "lines": [{"text": "one", "voice": "monk"}, {"text": "two", "voice": "chip"}]}), encoding="utf-8")
    r = runner.invoke(audio_app, ["beat", "demo", str(beat), "--sheet", str(tmp_path / "demo.yaml"),
                                  "--voices", str(tmp_path / "voices.yaml"), "--bpm", "120"])
    assert r.exit_code == 0, r.output
    out = tmp_path / "beat-demo-ru.mp3"
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:format_tags=comment",
                            "-of", "json", str(out)], capture_output=True, text=True, encoding="utf-8")
    info = json.loads(probe.stdout)["format"]
    assert float(info["duration"]) == pytest.approx(12.0, abs=0.1)
    assert info["tags"]["comment"] == GUIDE_TAG
    side = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    assert [ln["voice"] for ln in side["lines"]] == ["monk", "chip"]


# ── follow-ups found in real use ──────────────────────────────────────────────

def test_a_triplet_reading_is_not_an_off_tempo_render():
    from navig_audio.beats.catalog import tempo_matches, tempo_relation

    assert tempo_relation(92, 61.5) == "triplet"  # rattles read on the dotted pulse
    assert tempo_relation(150, 99.5) == "triplet"  # a 6/8 groove on a 150 beat
    assert tempo_relation(76, 152) == "half/double"
    assert tempo_relation(84, 83.5) == "same"
    assert tempo_relation(76, 82) is None and not tempo_matches(76, 82)


def test_a_block_spec_can_name_fallbacks():
    bm = BarMap(bpm=92, bar_s=240 / 92, phase_s=1.3, duration_s=155.0, levels_db=[0.0] * 59,
                blocks=[Block("full", 1, 54, 1.3, 142.2, 9), Block("tail", 55, 59, 142.2, 155.2, 0)])
    assert bm.block("build|full:1").label == "full"  # a render with no quiet intro
    with pytest.raises(ValueError):
        bm.block("build|drop")


def test_every_mix_scrubs_non_finite_samples_before_loudnorm():
    from navig_audio.beats.layer import FINITE

    g = layer_graph(80.0, [Occurrence("x", Path("x.wav"), 1.0)], Master())
    assert FINITE + ",loudnorm" in g
    vs = load_voices(VOICES)
    plan = plan_demo({"start_bar": 9, "lines": [{"text": "a", "voice": "monk"}]}, vs, _bm(), Path("."))
    d = demo_graph(plan, vs)
    assert d.count("aeval=") == 2 and FINITE + ",loudnorm" in d
