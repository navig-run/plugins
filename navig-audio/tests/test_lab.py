"""Audio Lab: delivery, restoration presets, video dialogue cleanup, matching, ambience.

Synthetic fixtures only (generated per test, never committed); no network.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
SR = 44100


def _wav(path: Path, x: np.ndarray, sr: int = SR) -> Path:
    x = np.atleast_2d(x)
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2").T.copy()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(x.shape[0])
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return path


def _tone(dur=6.0, amp=0.05, f=440.0) -> np.ndarray:
    t = np.arange(int(dur * SR)) / SR
    return amp * np.sin(2 * np.pi * f * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 0.5 * t))


def _speechlike(dur=5.0) -> np.ndarray:
    t = np.arange(int(dur * SR)) / SR
    f0 = 140 + 20 * np.sin(2 * np.pi * 0.7 * t)
    voice = sum((0.5 / k) * np.sin(2 * np.pi * k * np.cumsum(f0) / SR) for k in range(1, 12))
    return 0.35 * voice * (np.sin(2 * np.pi * 3.5 * t) > 0.2)


# ── delivery ─────────────────────────────────────────────────────────


def test_delivery_check_and_fix(tmp_path):
    from navig_audio.lab.delivery import check, fix

    src = _wav(tmp_path / "quiet.wav", _tone(amp=0.03))
    c = check(src, "podcast")
    assert c.verdict == "too-quiet" and not c.ok
    loud = check(src, "ebu-r128")
    assert loud.verdict in ("too-quiet", "pass", "too-loud")
    out, again = fix(src, "podcast", out_dir=tmp_path / "o")
    assert again.ok, again.to_dict()
    assert abs(again.lufs - -16.0) <= 1.0 and again.true_peak_db <= -0.95
    assert out.name.startswith("quiet.podcast") and src.exists()


def test_delivery_streaming_too_loud_is_not_a_failure_but_broadcast_is(tmp_path):
    from navig_audio.lab.delivery import check

    hot = _wav(tmp_path / "hot.wav", np.clip(_tone(amp=0.9) * 3, -0.8, 0.8))
    assert check(hot, "spotify").verdict in ("pass", "peaks-over")
    assert check(hot, "ebu-r128").verdict in ("too-loud", "peaks-over")


def test_delivery_unknown_target(tmp_path):
    from navig_audio.lab.delivery import DeliveryError, check

    with pytest.raises(DeliveryError, match="unknown target"):
        check(_wav(tmp_path / "a.wav", _tone()), "myspace")


# ── restoration presets ──────────────────────────────────────────────


def test_restoration_presets_ship_and_band_limits_plan(tmp_path):
    from navig_audio.enhance import analyze, plan
    from navig_audio.enhance import presets as presets_mod

    ids = {p["id"] for p in presets_mod._shipped()}
    assert {"audio-enhance/cassette", "audio-enhance/vinyl", "audio-enhance/phone-call",
            "audio-enhance/voice-memo"} <= ids
    rep = analyze(_wav(tmp_path / "m.wav", _speechlike()), category="speech")
    p = plan(rep, "studio", presets_mod.load("audio-enhance/voice-memo"))
    band = next(s for s in p.stages if s.name == "band")
    assert "highpass=f=80" in band.filter and "rumble below 80 Hz" in band.reason


# ── video dialogue cleanup ───────────────────────────────────────────


def _video(path: Path, audio: Path | None) -> Path:
    args = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25"]
    if audio is not None:
        args += ["-i", str(audio), "-shortest", "-c:a", "aac", "-b:a", "128k"]
    else:
        args += ["-t", "3"]
    subprocess.run([*args, "-c:v", "libx264", "-preset", "ultrafast", str(path)], check=True)
    return path


def _video_hash(path: Path) -> str:
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v", "-c", "copy", "-f", "md5", "-"],
                         capture_output=True, check=True)
    return out.stdout.decode().strip()


def test_clean_video_keeps_picture_and_sync(tmp_path):
    from navig_audio.lab.video import clean_video

    rng = np.random.default_rng(3)
    noisy = _wav(tmp_path / "v.wav", _speechlike() + rng.normal(0, 0.02, int(5.0 * SR)))
    vid = _video(tmp_path / "talk.mp4", noisy)
    # The synthetic "voice" is buzzy harmonics, not speech, so pin the ffmpeg engine: this
    # test is about the video plumbing (DeepFilterNet rightly mangles non-speech).
    res = clean_video(vid, out_dir=tmp_path / "o", params={"engine": "local"})
    assert res.verdict in ("pass", "warn"), res.enhance
    assert _video_hash(Path(res.output)) == _video_hash(vid)  # the picture is bit-for-bit the same
    assert res.sync["ok"] and res.sync["drift_s"] <= 0.05


def test_clean_video_writes_nothing_when_the_cleanup_is_rejected(tmp_path, monkeypatch):
    import navig_audio.lab.video as video_mod
    from navig_audio.enhance.engine import enhance as real_enhance

    def rejected(*a, **kw):
        r = real_enhance(*a, **kw)
        r.verdict, r.outputs = "fail", []
        return r

    monkeypatch.setattr(video_mod, "enhance", rejected)
    rng = np.random.default_rng(4)
    vid = _video(tmp_path / "talk.mp4", _wav(tmp_path / "v.wav", _speechlike() + rng.normal(0, 0.02, int(5.0 * SR))))
    res = video_mod.clean_video(vid, out_dir=tmp_path / "o", params={"engine": "local"})
    assert res.verdict == "fail" and res.output is None
    assert not list((tmp_path / "o").glob("*.cleaned.*"))


def test_clean_video_refuses_silent_video_and_audio_files(tmp_path):
    from navig_audio.lab.video import VideoCleanError, clean_video

    with pytest.raises(VideoCleanError, match="no sound"):
        clean_video(_video(tmp_path / "mute.mp4", None))
    with pytest.raises(VideoCleanError, match="not a video"):
        clean_video(_wav(tmp_path / "a.wav", _tone()))


# ── reference matching ───────────────────────────────────────────────


def test_match_moves_tone_and_loudness_toward_the_reference(tmp_path):
    from navig_audio.lab.match import MatchError, match_reference

    rng = np.random.default_rng(5)
    bright = rng.normal(0, 0.05, int(6 * SR))
    dark = np.convolve(rng.normal(0, 0.2, int(6 * SR)), np.ones(40) / 40, mode="same")
    src, ref = _wav(tmp_path / "bright.wav", bright), _wav(tmp_path / "dark.wav", dark)
    res = match_reference(src, ref, out_dir=tmp_path / "o")
    assert res.tone_distance_after_db < res.tone_distance_before_db
    assert abs(res.lufs_after - res.lufs_reference) <= 1.0
    assert all(abs(v) <= 6.0 for v in res.eq_db.values())
    with pytest.raises(MatchError):
        match_reference(src, ref, strength=1.5)


# ── ambience ─────────────────────────────────────────────────────────


def test_local_ambience_loops_seamlessly_at_length(tmp_path):
    from navig_audio.lab.ambience import ambience
    from navig_audio.enhance.analysis import probe

    res = ambience("rain", seconds=30, out_dir=tmp_path, loop_seconds=10)
    assert res.seam["smooth"], res.seam
    assert abs(probe(Path(res.render))["duration_s"] - 30) < 0.1
    assert res.source == "synth"


def test_ambience_guards(tmp_path):
    from navig_audio.lab.ambience import AmbienceError, ambience

    with pytest.raises(AmbienceError, match="local beds"):
        ambience("night café", seconds=30, out_dir=tmp_path)
    with pytest.raises(AmbienceError, match="allow cloud"):
        ambience("night café", seconds=30, out_dir=tmp_path, source="generate")


def test_generated_ambience_uses_the_provider_once(tmp_path):
    from navig_audio.lab.ambience import ambience

    class Fake:
        calls = []

        async def sfx(self, prompt, duration_s):
            self.calls.append((prompt, duration_s))
            buf = io.BytesIO()
            rng = np.random.default_rng(9)
            x = (rng.normal(0, 0.05, int(duration_s * SR)) * 32767).astype("<i2")
            with wave.open(buf, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SR)
                w.writeframes(x.tobytes())
            return buf.getvalue()

    fake = Fake()
    res = ambience("night café, distant chatter", seconds=45, out_dir=tmp_path, source="generate",
                   allow_cloud=True, provider=fake)
    assert len(fake.calls) == 1 and "night café" in fake.calls[0][0] and fake.calls[0][1] == 22.0
    assert res.source == "generated" and Path(res.render).is_file()


# ── CLI ──────────────────────────────────────────────────────────────


def test_cli_delivery_all_json(tmp_path):
    from typer.testing import CliRunner

    from navig_audio.commands.audio import audio_app

    src = _wav(tmp_path / "a.wav", _tone(amp=0.03))
    res = CliRunner().invoke(audio_app, ["delivery", str(src), "--target", "all", "--json"])
    data = json.loads(res.output)
    assert {c["target"] for c in data["checks"]} >= {"youtube", "ebu-r128", "podcast"}
    assert res.exit_code == 1  # not every target passes


# ── podcast auto-edit ────────────────────────────────────────────────


def _words(*spec):
    return [{"word": w, "start": s, "end": e} for w, s, e in spec]


def test_podcast_cuts_fillers_pauses_repeats_and_merges_overlaps():
    from navig_audio.lab.podcast_edit import propose_cuts

    words = _words(("So", 0.0, 0.3), ("um,", 0.4, 0.7), ("the", 0.9, 0.95), ("the", 1.0, 1.1), ("idea", 1.2, 1.5),
                   ("is", 4.0, 4.2), ("simple.", 4.3, 4.8))
    cuts = propose_cuts(words, "en", 5.0)
    kinds = [c.kind for c in cuts]
    assert kinds == ["filler", "repeat", "pause"], cuts
    filler, repeat, pause = cuts
    assert filler.start == 0.4 and filler.end <= 0.9           # never into the next word
    assert (repeat.start, repeat.end) == (0.9, 1.0)             # the first "the" goes, the second stays
    assert abs((4.0 - 1.5) - pause.seconds - 0.5) < 1e-6        # 0.5 s of the pause is kept
    assert not propose_cuts(words, "en", 5.0, cut_fillers=False, cut_repeats=False, max_pause=10)


def test_podcast_french_fillers_and_cutlist_roundtrip():
    from navig_audio.lab.podcast_edit import CutList, PodcastEditError, propose_cuts

    cuts = propose_cuts(_words(("Alors", 0, 0.4), ("euh", 0.5, 0.9), ("voilà", 1.0, 1.4)), "fr", 2.0)
    assert [c.text for c in cuts] == ["euh"]
    cl = CutList("x.wav", "fr", 2.0, cuts)
    back = CutList.from_dict(json.loads(json.dumps(cl.to_dict())))
    assert back.to_dict() == cl.to_dict()
    with pytest.raises(PodcastEditError):
        CutList.from_dict({"cuts": [{"start": "a"}]})


def test_podcast_apply_removes_exactly_the_cuts_and_respects_keep(tmp_path):
    from navig_audio.enhance.analysis import probe
    from navig_audio.lab.podcast_edit import CutList, Cut, apply_cuts

    src = _wav(tmp_path / "ep.wav", _tone(dur=10.0))
    cl = CutList(str(src), "en", 10.0, [Cut(2.0, 3.0, "filler", "um"), Cut(5.0, 7.0, "pause", "2s"),
                                         Cut(8.0, 9.0, "repeat", "kept", keep=True)])
    dst, removed = apply_cuts(cl, out_dir=tmp_path / "o")
    assert abs(removed - 3.0) < 0.05, removed
    assert abs(probe(dst)["duration_s"] - 7.0) < 0.05
    assert dst.name == "ep.edited.wav" and src.exists()
    assert json.loads((dst.parent / (dst.name + ".cuts.json")).read_text(encoding="utf-8"))["removable_s"] == 3.0


def test_podcast_apply_has_no_clicks_at_the_joins(tmp_path):
    from navig_audio.enhance.analysis import decode, probe
    from navig_audio.lab.podcast_edit import CutList, Cut, apply_cuts

    src = _wav(tmp_path / "t.wav", 0.3 * np.sin(2 * np.pi * 220 * np.arange(4 * SR) / SR))
    dst, _ = apply_cuts(CutList(str(src), "en", 4.0, [Cut(1.0013, 2.0071, "pause", "")]), out_dir=tmp_path)
    y = decode(dst, probe(dst)["sample_rate"], 1)[0]
    # A hard cut mid-cycle jumps by up to 0.6; a crossfade keeps the step near the tone's own slope.
    assert np.abs(np.diff(y)).max() < 0.05


# ── stem studio ──────────────────────────────────────────────────────


def _zip_of(stems: dict[str, np.ndarray]) -> bytes:
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, x in stems.items():
            wb = io.BytesIO()
            with wave.open(wb, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SR)
                w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
            z.writestr(f"song/{name}.wav", wb.getvalue())
        z.writestr("__MACOSX/._junk", b"")
    return buf.getvalue()


class _FakeStems:
    def __init__(self):
        self.calls = 0

    async def stems(self, src, *, six):
        self.calls += 1
        return _zip_of({"vocals": _tone(3, 0.2, 300), "drums": _tone(3, 0.2, 80), "bass": _tone(3, 0.2, 55),
                        "other": _tone(3, 0.2, 900)})


def test_stems_split_needs_consent_then_mixes_locally(tmp_path):
    from navig_audio.enhance.analysis import loudness
    from navig_audio.lab.stems import StemsError, mix, split

    song = _wav(tmp_path / "song.wav", _tone(3))
    with pytest.raises(StemsError, match="allow cloud"):
        split(song)
    fake = _FakeStems()
    res = split(song, allow_cloud=True, provider=fake, out_dir=tmp_path)
    assert fake.calls == 1 and set(res.stems) == {"vocals", "drums", "bass", "other"}
    karaoke = mix(res.folder, preset="karaoke")
    acapella = mix(res.folder, preset="acapella")
    assert karaoke.name == "karaoke.wav" and acapella.exists()
    quiet = mix(res.folder, solo=["vocals"], gains={"voc": -12}, out=tmp_path / "q.wav")
    loud = mix(res.folder, solo=["vocals"], out=tmp_path / "v.wav")
    assert abs((loudness(loud)[0] - loudness(quiet)[0]) - 12) < 0.5
    with pytest.raises(StemsError, match="no stem named"):
        mix(res.folder, mute=["piano"])
    with pytest.raises(StemsError, match="nothing to play"):
        mix(res.folder, mute=["vocals", "drums", "bass", "other"])


def test_stems_rejects_a_reply_with_no_audio(tmp_path):
    from navig_audio.lab.stems import StemsError, split

    class Empty:
        async def stems(self, src, *, six):
            return b"not a zip"

    with pytest.raises(StemsError, match="zip"):
        split(_wav(tmp_path / "s.wav", _tone(1)), allow_cloud=True, provider=Empty(), out_dir=tmp_path)
    assert not list(tmp_path.glob("*.stems"))


# ── sound-alike search ───────────────────────────────────────────────


def test_similar_ranks_the_same_kind_of_sound_first(tmp_path, monkeypatch):
    import navig_audio.lab.similar as sim

    monkeypatch.setattr(sim, "_index_path", lambda folder: tmp_path / "index.npz")
    lib = tmp_path / "lib"
    lib.mkdir()
    rng = np.random.default_rng(1)
    for i in range(3):
        _wav(lib / f"hiss{i}.wav", rng.normal(0, 0.1 + 0.02 * i, 3 * SR))
        _wav(lib / f"hum{i}.wav", _tone(3, 0.2, 60 + 10 * i))
    query = _wav(tmp_path / "q.wav", rng.normal(0, 0.12, 3 * SR))
    matches, computed = sim.search(query, lib, top=3)
    assert computed == 6
    assert all("hiss" in Path(m.path).name for m in matches), [m.to_dict() for m in matches]
    _, again = sim.search(query, lib, top=3)
    assert again == 0  # cached
    (tmp_path / "empty").mkdir()
    with pytest.raises(sim.SimilarError, match="no audio"):
        sim.search(query, tmp_path / "empty")


# ── dubbing ──────────────────────────────────────────────────────────


class _FakeDub:
    def __init__(self, stretch=1.0, fail=()):
        self.stretch, self.fail, self.uploads, self.polls = stretch, set(fail), [], 0

    async def start(self, src, target_lang, source_lang, num_speakers):
        self.uploads.append((Path(src).suffix, target_lang))
        return f"id-{target_lang}"

    async def status(self, dubbing_id):
        self.polls += 1
        lang = dubbing_id.split("-")[1]
        if lang in self.fail:
            return {"status": "failed", "error": "unsupported language"}
        return {"status": "dubbed" if self.polls > 1 else "dubbing"}

    async def download(self, dubbing_id, lang):
        buf = io.BytesIO()
        x = (_tone(5.0 * self.stretch, 0.2, 330) * 32767).astype("<i2")
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(x.tobytes())
        return buf.getvalue()


def test_dub_video_uploads_sound_only_and_keeps_picture_and_original_track(tmp_path):
    from navig_audio.lab.dub import DubError, dub

    vid = _video(tmp_path / "talk.mp4", _wav(tmp_path / "v.wav", _speechlike()))
    with pytest.raises(DubError, match="allow cloud"):
        dub(vid, ["fr"])
    fake = _FakeDub(fail={"xx"})
    res = dub(vid, ["fr", "xx"], allow_cloud=True, provider=fake, out_dir=tmp_path / "o", poll_s=0)
    assert all(suffix == ".mp3" for suffix, _ in fake.uploads)          # never the video itself
    assert [t.language for t in res.takes] == ["fr"] and "unsupported" in res.failed["xx"]
    out = Path(res.takes[0].output)
    assert out.name == "talk.fr.mp4" and _video_hash(out) == _video_hash(vid)
    tracks = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                             "-of", "csv=p=0", str(out)], capture_output=True, text=True).stdout.split()
    assert len(tracks) == 2 and res.takes[0].in_sync
    assert json.loads(Path(res.sidecar).read_text(encoding="utf-8"))["generated"] is True


def test_dub_reports_a_dub_that_drifts(tmp_path):
    from navig_audio.lab.dub import dub

    src = _wav(tmp_path / "a.wav", _speechlike())
    res = dub(src, ["de"], allow_cloud=True, provider=_FakeDub(stretch=1.2), out_dir=tmp_path, poll_s=0)
    assert not res.takes[0].in_sync and res.takes[0].drift > 0.15


# ── score to picture ─────────────────────────────────────────────────


def test_scenes_merge_short_shots_and_split_long_ones():
    from navig_audio.lab.score import build_plan, scenes_from_cuts

    scenes = scenes_from_cuts([4.0, 5.0, 12.0], 20.0)
    assert scenes == [(0.0, 5.0), (5.0, 12.0), (12.0, 20.0)]  # the 1 s shot joined its neighbour
    assert len(scenes_from_cuts([], 250.0)) == 3                 # 250 s → three sections under 120 s
    plan = build_plan(scenes, "dark synth, slow")
    assert sum(s["duration_ms"] for s in plan["sections"]) == 20000
    assert plan["positive_global_styles"][:2] == ["dark synth", "slow"]


def _cut_video(path: Path, with_sound: bool) -> Path:
    args = ["ffmpeg", "-v", "error", "-y"]
    for c, d in (("red", 4), ("blue", 5), ("green", 6)):
        args += ["-f", "lavfi", "-i", f"color=c={c}:size=320x180:rate=25:d={d}"]
    if with_sound:
        args += ["-f", "lavfi", "-i", "sine=f=200:d=15"]
    args += ["-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]", "-map", "[v]"]
    if with_sound:
        args += ["-map", "3:a", "-c:a", "aac"]
    subprocess.run([*args, "-c:v", "libx264", "-preset", "ultrafast", "-g", "25", str(path)], check=True)
    return path


class _FakeComposer:
    def __init__(self):
        self.plans = []

    async def compose(self, plan):
        self.plans.append(plan)
        buf = io.BytesIO()
        x = (_tone(14.0, 0.3, 220) * 32767).astype("<i2")  # a little short: must be padded to the picture
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(x.tobytes())
        return buf.getvalue()


def test_score_follows_the_cuts_and_keeps_the_picture(tmp_path):
    from navig_audio.enhance.analysis import probe
    from navig_audio.lab.score import ScoreError, score_to_picture

    vid = _cut_video(tmp_path / "cuts.mp4", with_sound=True)
    dry = score_to_picture(vid, "dark synth", dry_run=True)
    assert [round(c) for c in dry.cuts] == [4, 9], dry.cuts
    with pytest.raises(ScoreError, match="allow cloud"):
        score_to_picture(vid, "dark synth")
    fake = _FakeComposer()
    res = score_to_picture(vid, "dark synth", allow_cloud=True, provider=fake, out_dir=tmp_path / "o")
    assert [s["duration_ms"] for s in fake.plans[0]["sections"]] == [4000, 5000, 6000]
    assert res.ducked and _video_hash(Path(res.output)) == _video_hash(vid)
    assert abs(probe(Path(res.music))["duration_s"] - 15.0) < 0.05


def test_score_refuses_short_or_pictureless_input(tmp_path):
    from navig_audio.lab.score import ScoreError, score_to_picture

    with pytest.raises(ScoreError, match="not a video"):
        score_to_picture(_wav(tmp_path / "a.wav", _tone(12)), "x", dry_run=True)
    with pytest.raises(ScoreError, match="must be"):
        score_to_picture(_video(tmp_path / "s.mp4", None), "x", dry_run=True)


def test_no_two_audio_commands_share_a_name():
    """A second @audio_app.command("x") silently shadows the first — the CLI contract breaks."""
    from collections import Counter

    from navig_audio.commands.audio import audio_app

    names = Counter(c.name or c.callback.__name__ for c in audio_app.registered_commands)
    names.update(g.name for g in audio_app.registered_groups)  # a sub-app named like a command swallows it
    assert not [n for n, k in names.items() if k > 1], names


@pytest.mark.parametrize("name", ["rain", "wind", "server-room", "room-tone", "pink", "brown", "white"])
def test_every_local_bed_renders_and_loops(tmp_path, name):
    from navig_audio.lab.ambience import ambience

    res = ambience(name, seconds=24, out_dir=tmp_path, loop_seconds=10)
    assert Path(res.render).is_file() and res.seam["smooth"], (name, res.seam)
