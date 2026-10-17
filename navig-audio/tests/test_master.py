"""`navig audio master` / `loudness` — the mastering pipeline, end to end on synthetic audio.

The fixtures are made by ffmpeg itself: a pulsed 300 Hz tone stands in for a voice (it has
gaps, so the duck and the gating meter both have something to do) and a 55 Hz bass with a
click track stands in for a beat. What is proven is what a user relies on: the master lands
on the loudness target, under the true-peak ceiling, at the length of the song (not of the
vocal), and the vocal is not buried under the beat.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

pytest.importorskip("navig_media_core.media.audio_edit", reason="navig-generate not installed")

from navig_audio.commands.audio import audio_app  # noqa: E402
from navig_audio.mastering.chain import PRESETS, VOICE_TONES, build_mix_graph, preset, vocal_chain  # noqa: E402
from navig_audio.mastering.loudness import measure  # noqa: E402
from navig_audio.mastering.master import master, unique_path  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
                                  reason="ffmpeg/ffprobe not on PATH")
runner = CliRunner()


def _lavfi(dst: Path, expr: str, seconds: float) -> Path:
    subprocess.run([shutil.which("ffmpeg"), "-nostdin", "-y", "-f", "lavfi", "-i", expr, "-t", str(seconds),
                    "-ac", "2", str(dst)], capture_output=True, check=True, timeout=120)
    return dst


@pytest.fixture()
def vocal(tmp_path: Path) -> Path:
    # a "phrase" every second: 0.6 s of tone, 0.4 s of silence — quiet, like a real dry take
    return _lavfi(tmp_path / "vocal.wav",
                  "aevalsrc=0.05*sin(2*PI*300*t)*lt(mod(t\\,1)\\,0.6)+0.01*sin(2*PI*900*t)*lt(mod(t\\,1)\\,0.6):s=44100",
                  6)


@pytest.fixture()
def beat(tmp_path: Path) -> Path:
    # 55 Hz bass + a click on every beat at 120 bpm, louder than the vocal
    return _lavfi(tmp_path / "beat.wav",
                  "aevalsrc=0.4*sin(2*PI*55*t)+0.6*lt(mod(t\\,0.5)\\,0.01)*sin(2*PI*2000*t):s=44100", 10)


def _duration(p: Path) -> float:
    out = subprocess.run([shutil.which("ffprobe"), "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(p)], capture_output=True, text=True, timeout=60).stdout
    return float(out.strip())


@needs_ffmpeg
def test_loudness_reads_a_known_level(tmp_path: Path):
    # A 1 kHz sine at amplitude 0.1 (-20 dBFS peak) in both channels reads -20 LUFS: the
    # K-weighting is ~0 dB at 1 kHz and stereo sums the channels. (lavfi's `sine` source is at
    # 1/8 amplitude by default, and a mono source up-mixed to stereo loses 3 dB — hence a
    # two-channel aevalsrc with an explicit amplitude.)
    f = _lavfi(tmp_path / "s.wav", "aevalsrc=0.1*sin(2*PI*1000*t)|0.1*sin(2*PI*1000*t):s=44100", 5)
    got = measure(f)
    assert -21.5 <= got.lufs <= -18.5, got
    assert -21 <= got.true_peak <= -19, got


@pytest.mark.parametrize("name", list(PRESETS))
def test_every_preset_builds_a_graph(name: str):
    g = build_mix_graph(preset(name), offset_s=1.5, total_s=30, beat_level=-22)
    assert g.endswith("[mix]") and "[vkey]" in g and "adelay=delays=1500" in g and "apad=whole_dur=30.000" in g
    # space must never scale the dry voice down (out_gain < 1 buried it 19 LU once)
    for part in preset(name).space.split(","):
        if part.startswith("aecho="):
            in_gain, out_gain = part[len("aecho="):].split(":")[:2]
            assert float(in_gain) == 1 and float(out_gain) == 1


def test_vocal_ready_skips_the_chain():
    # an already-processed voice is levelled and blended — never compressed / boosted / echoed twice
    g = build_mix_graph(preset("ritual"), ready=True, beat_level=-22)
    vocal = g.split(";")[0]
    assert "loudnorm" in vocal
    for stage in ("acompressor", "deesser", "equalizer", "aecho", "afftdn", "asoftclip"):
        assert stage not in vocal, stage


@pytest.mark.parametrize("name", ["ritual", "boom-bap", "collage"])
def test_song_presets_sit_the_voice_inside_the_music(name: str):
    # calibrated against released mixes (voice 1.4-3.1 LU under the music): never above the beat
    assert preset(name).balance_lu < 0 and preset(name).duck_ratio <= 1.5


@pytest.mark.parametrize("tone", sorted(VOICE_TONES))
@pytest.mark.parametrize("ready", [True, False])
def test_voice_tone_colours_the_voice_last(tone: str, ready: bool):
    # the colour comes after levelling (ready) or the whole chain — it must not be re-levelled away
    chain = vocal_chain(preset("boom-bap"), ready=ready, gain_db=-1.0, tone=tone)
    assert chain.endswith(VOICE_TONES[tone])
    assert chain.index("volume=-1.0dB") < chain.index("lowpass=")


def test_no_voice_tone_leaves_the_chain_alone():
    p = preset("boom-bap")
    assert vocal_chain(p, ready=True, tone="none") == vocal_chain(p, ready=True)
    assert "lowpass" not in build_mix_graph(p, ready=True, tone=None).split(";")[0]


def test_unknown_voice_tone_is_refused():
    with pytest.raises(ValueError, match="dark"):
        vocal_chain(preset("ritual"), tone="sparkly")


def test_cli_unknown_voice_tone_exits_2(tmp_path: Path):
    v = tmp_path / "v.wav"
    v.write_bytes(b"x")
    r = runner.invoke(audio_app, ["master", str(v), "--beat", str(v), "--voice-tone", "sparkly"])
    assert r.exit_code == 2


def test_duck_off_still_builds():
    g = build_mix_graph(preset("clean"), duck_ratio=1)
    assert "sidechaincompress" not in g and "[ducked]" in g


@needs_ffmpeg
@pytest.mark.parametrize("name", ["ritual", "clean"])
def test_master_hits_the_target_at_song_length(tmp_path: Path, vocal: Path, beat: Path, name: str):
    p = preset(name)
    res = master(vocal, beat, tmp_path / f"song.{name}.mp3", p)
    assert res.out.exists() and res.sidecar.exists()
    assert abs(res.lufs - p.lufs) <= 1.0, res
    assert res.true_peak <= -1.0 + 0.5, res
    # the song is as long as the beat — not cut off when the vocal ends (sidechain key padding)
    assert abs(_duration(res.out) - 10) <= 0.3
    side = json.loads(res.sidecar.read_text(encoding="utf-8"))
    # the beat was set under the measured vocal by the preset's balance
    assert abs(side["processed_vocal_lufs"] - side["beat_level_lufs"] - p.balance_lu) < 0.01
    assert side["graph"].startswith("[0:a]")
    assert side["voice_tone"] is None


@needs_ffmpeg
def test_encoded_true_peak_honours_the_ceiling(tmp_path: Path, vocal: Path):
    # A dense, square-ish beat makes the mp3 encode overshoot a sample-peak limiter; the
    # ENCODED file must still be under the ceiling (re-limited until it is). Found on a real
    # beat: TP -0.32 dBTP with a fixed 0.8 dB margin.
    hot = _lavfi(tmp_path / "hot.wav",
                 "aevalsrc=0.9*sgn(sin(2*PI*110*t))+0.4*sgn(sin(2*PI*3300*t)):s=44100", 8)
    res = master(vocal, hot, tmp_path / "hot.mp3", preset("ritual"), tp=-1.0)
    assert res.true_peak <= -1.0 + 0.05, res
    assert abs(res.lufs - preset("ritual").lufs) <= 1.0, res


@needs_ffmpeg
def test_offset_extends_the_song(tmp_path: Path, vocal: Path, beat: Path):
    res = master(vocal, beat, tmp_path / "late.mp3", preset("clean"), offset_s=7)
    assert abs(_duration(res.out) - 13) <= 0.3  # 7 s offset + 6 s vocal > 10 s beat


def test_unique_path_never_overwrites(tmp_path: Path):
    a = tmp_path / "x.master.mp3"
    a.write_bytes(b"1")
    assert unique_path(a).name == "x.master-take2.mp3"


@needs_ffmpeg
def test_cli_dry_run_writes_nothing(tmp_path: Path, vocal: Path, beat: Path):
    r = runner.invoke(audio_app, ["master", str(vocal), "--beat", str(beat), "--dry-run", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["preset"] == "ritual"
    assert not list(tmp_path.glob("*.master*.mp3"))


def test_cli_unknown_preset_exits_2(tmp_path: Path):
    v = tmp_path / "v.wav"
    v.write_bytes(b"x")
    r = runner.invoke(audio_app, ["master", str(v), "--beat", str(v), "--preset", "nope"])
    assert r.exit_code == 2


def test_cli_ref_without_matchering_exits_2(tmp_path: Path, monkeypatch):
    import builtins

    real = builtins.__import__

    def no_matchering(name, *a, **k):
        if name == "matchering":
            raise ImportError("no")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_matchering)
    v = tmp_path / "v.wav"
    v.write_bytes(b"x")
    r = runner.invoke(audio_app, ["master", str(v), "--beat", str(v), "--ref", str(v)])
    assert r.exit_code == 2


@needs_ffmpeg
def test_cli_loudness_json(tmp_path: Path, beat: Path):
    r = runner.invoke(audio_app, ["loudness", str(beat), "--json"])
    assert r.exit_code == 0, r.output
    row = json.loads(r.output)[0]
    assert {"lufs", "lra", "true_peak"} <= set(row)


@needs_ffmpeg
def test_relative_paths_resolve_against_the_invocation_dir(tmp_path, monkeypatch, vocal, beat):
    """A typed path follows the OPERATOR, not navig's chdir into the active space.

    The old signature validated `exists=True` against the process cwd (the space inside
    navig), so `navig audio master v.wav --beat b.wav` refused files sitting right in the
    directory it was typed from. Anchoring is NAVIG_INVOCATION_CWD, cwd standalone.
    """
    typed = tmp_path / "typed"
    typed.mkdir()
    shutil.copy(vocal, typed / "v.wav")
    shutil.copy(beat, typed / "b.wav")
    elsewhere = tmp_path / "elsewhere"   # no v.wav/b.wav here — the space stand-in
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("NAVIG_INVOCATION_CWD", str(typed))
    res = runner.invoke(audio_app, ["master", "v.wav", "--beat", "b.wav", "--dry-run"])
    assert res.exit_code == 0, res.output
    # …and a genuinely missing file still fails with the same clear error class
    res2 = runner.invoke(audio_app, ["master", "v.wav", "--beat", "nope.wav", "--dry-run"])
    assert res2.exit_code == 2 and "not found" in res2.output
