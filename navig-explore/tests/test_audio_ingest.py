"""`navig explore audio-ingest` — classify, name, convert, keep originals, undo.

Fixtures are short tones written by ffmpeg under rip-style names; what is proven is what a
user relies on: the right folder from the filename, a readable name with the platform junk
gone, a real mp3 at the loudness asked for, the original kept, and undo putting it all back.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from navig_explore import audio_ingest as ai

needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
                                  reason="ffmpeg/ffprobe not on PATH")


def _clip(path: Path, seconds: float = 3, freq: int = 440) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.m4a")
    subprocess.run([shutil.which("ffmpeg"), "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i",
                    f"aevalsrc=0.2*sin(2*PI*{freq}*t):s=44100", "-t", str(seconds), "-c:a", "aac", str(tmp)],
                   check=True, timeout=60)
    tmp.replace(path)  # keep the rip's real (sometimes wrong) extension
    return path


@pytest.mark.parametrize("stem,expected", [
    ("Feel_the_voice_of_Nature_#throatsinging_#alexkuula_7508306146298", "feel the voice of nature throatsinging alexkuula"),
    ("TikTok video #7457959599816052014-7457959599816052014", ""),
    ("original sound - geofr__1724725144_musicaldown.com", "geofr"),
    ("e4e06e4af0ac83674d436a2a8368faff26f97b6187832c14d2f0097128c53760", ""),
    ("audio (3)", ""),
    ("SaveTik.co_7384750951237700871", ""),
    ("FDownloader.net-901217511434515-(320kbps)", ""),
    ("How_long_can_you_listen_#foryou_#fyp_#foryourpage_#healingtiktok", "How long can you listen healingtiktok"),
])
def test_clean_name_strips_platform_junk(stem: str, expected: str):
    assert ai.clean_name(stem).lower() == expected.lower()


@pytest.mark.parametrize("stem,cat", [
    ("Feel_the_voice_of_Nature_#throatsinging_#alexkuula_7508306146298", "voice-singing"),
    ("Gyuto_Tantric_Monastery_is_one_of_the_most_revered_7503845717388", "voice-singing"),
    ("Three_wattled_bellbird_Procnias_tricarunculatus_75069175180621", "nature"),
    ("#backhoeloader_#machinehire_#siteclearance_#tiktok_7531859921206", "machines"),
    ("#binauralbeats_#meditation_#concentration_#awakeni_7467905705350", "drones-tones"),
    ("Rules_of_power_101_ psychologicalquotes_ powerful_7513253900720", "voice-speech"),
    ("SOAD_versi_kearifan_lokal_#systemofadown_#toxicity_7528160785059", "songs"),
])
def test_keywords_route_rips(stem: str, cat: str):
    got, conf, reason = ai.classify(stem, None, "", ai.DEFAULT_RULES)
    assert got == cat, reason
    assert conf >= 0.7


@pytest.mark.parametrize("stem,cat", [
    ("@maedchenchorhamburg_bringing_the_fire,_literally_7533512345", "voice-singing"),
    ("#onthisday #tornado #tornadosiren #siren #scary #creepy -7512345678901", "fx"),
    ("#звуккосмоса_#космос_#quasar_7477662162299751702", "ambience"),
    ("Activate_Brain_to_100__Potential", "drones-tones"),
])
def test_compound_hashtags_and_new_keywords(stem: str, cat: str):
    assert ai.classify(stem, None, "", ai.DEFAULT_RULES)[0] == cat


def test_short_keyword_does_not_fire_inside_a_longer_tag():
    assert ai.keyword_scores("#author_123456789", "", ai.DEFAULT_RULES)["fx"] == 0


def test_implausible_transcript_is_dropped():
    assert ai.plausible("වවසිවුසිා වවසුළුරා") == ""
    assert ai.plausible("Как можно здесь жить?") == "Как можно здесь жить?"
    assert ai.plausible("Tu tiens parce que tu es fort") == "Tu tiens parce que tu es fort"


def test_tonal_clip_without_speech_is_music():
    cat, conf, reason = ai.classify("TikTok video #7524664512817974550", {"speech_ratio": 0.01, "beat_strength": 0.2,
                                                                       "harmonicity_mean": 0.77}, "", ai.DEFAULT_RULES)
    assert cat == "music" and conf >= ai.MIN_CONF and "tonal" in reason


def test_some_speech_plus_a_real_transcript_is_speech():
    cat, _, reason = ai.classify("Prix_actuel_111979", {"speech_ratio": 0.43}, "Hello Bitcoiners thank you very much",
                                 ai.DEFAULT_RULES)
    assert cat == "voice-speech" and "transcript" in reason


@needs_ffmpeg
def test_replan_uses_the_cache(tmp_path: Path, monkeypatch):
    src, lib = tmp_path / "rips", tmp_path / "library"
    _clip(src / "monk_chant_111111111.m4a")
    ai.plan(src, lib, rules=ai.DEFAULT_RULES, use_audio=True)
    from navig_explore import audio_class as ac

    def boom(*a, **k):
        raise AssertionError("decoded again — the cache was not used")

    monkeypatch.setattr(ac, "decode_window", boom)
    items = ai.plan(src, lib, rules=ai.DEFAULT_RULES, use_audio=True)
    assert items[0].category == "voice-singing"


def test_no_evidence_goes_to_review():
    cat, conf, _ = ai.classify("audio (4)", None, "", ai.DEFAULT_RULES)
    assert cat == "unknown" and conf < ai.MIN_CONF


def test_speech_label_without_speech_loses_confidence():
    _, alone, _ = ai.classify("motivation_quote_123456789", None, "", ai.DEFAULT_RULES)
    _, conf, reason = ai.classify("motivation_quote_123456789", {"speech_ratio": 0.0}, "", ai.DEFAULT_RULES)
    assert conf < alone and "speech_ratio" in reason


def test_audio_evidence_alone_can_decide():
    cat, conf, _ = ai.classify("hash_only_00000000000", {"speech_ratio": 0.8}, "", ai.DEFAULT_RULES)
    assert cat == "voice-speech" and conf >= ai.MIN_CONF


def test_rules_file_extends(tmp_path: Path):
    pytest.importorskip("yaml")
    f = tmp_path / "r.yaml"
    f.write_text("ritual: [drum, shaman]\n", encoding="utf-8")
    rules = ai.load_rules(f)
    assert ai.classify("#shaman_drum_123456789", None, "", rules)[0] == "ritual"


@needs_ffmpeg
def test_plan_apply_undo_round_trip(tmp_path: Path):
    src, lib = tmp_path / "rips", tmp_path / "library"
    a = _clip(src / "Feel_the_voice_of_Nature_#throatsinging_7508306146298.m4a")
    b = _clip(src / "sub" / "application (6).octet-stream")  # really audio, wrong extension
    c = _clip(src / "#binauralbeats_#meditation_7467905705350.m4a", freq=220)
    items = ai.plan(src, lib, rules=ai.DEFAULT_RULES, use_audio=False)
    by = {Path(i.src).name: i for i in items}
    assert by[a.name].dest.endswith(str(Path("voice-singing") / "feel-the-voice-of-nature-throatsinging.mp3"))
    assert "_review" in by[b.name].dest  # no keyword, no audio evidence asked for
    stats = ai.apply(items, lib, lufs=-16)
    assert stats["ingested"] == 3
    out = Path(by[c.name].dest)
    assert out.exists() and out.suffix == ".mp3"
    assert not a.exists() and (lib / "_originals" / a.name).exists()  # moved, not lost
    assert (lib / "_originals" / "sub" / b.name).exists()
    assert (lib / "INDEX.csv").exists() and "voice-singing" in (lib / "README.md").read_text(encoding="utf-8")
    log = lib / ".mediaexplorer" / ai.LOG
    assert all("md5" in json.loads(line) for line in log.read_text(encoding="utf-8").splitlines())
    back = ai.undo(log)
    assert back["restored"] == 3 and a.exists() and b.exists() and c.exists()
    assert not out.exists()


@needs_ffmpeg
def test_name_collision_gets_a_suffix(tmp_path: Path):
    src, lib = tmp_path / "rips", tmp_path / "library"
    _clip(src / "monk_chant_111111111.m4a")
    _clip(src / "monk_chant_222222222.m4a")
    names = sorted(Path(i.dest).name for i in ai.plan(src, lib, rules=ai.DEFAULT_RULES, use_audio=False))
    assert names == ["monk-chant-2.mp3", "monk-chant.mp3"]


@needs_ffmpeg
def test_copy_mode_keeps_the_source(tmp_path: Path):
    src, lib = tmp_path / "rips", tmp_path / "library"
    a = _clip(src / "monk_chant_111111111.m4a")
    items = ai.plan(src, lib, rules=ai.DEFAULT_RULES, use_audio=False)
    ai.apply(items, lib, originals="copy")
    assert a.exists() and (lib / "_originals" / a.name).exists()
    ai.undo(lib / ".mediaexplorer" / ai.LOG)
    assert a.exists() and not (lib / "_originals" / a.name).exists()
