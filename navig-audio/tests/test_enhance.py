"""Audio Enhance: analysis, planning, verification and the engine on synthetic audio.

Fixtures are generated per test (numpy → WAV via the stdlib), never committed. Each
defect fixture must produce its finding and its stage; the clean fixture must produce
an EMPTY plan; and verification must reject outputs that were destroyed in the ways a
bad engine destroys audio (reversed, truncated, pitch-shifted, events injected).
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import threading
import wave
from pathlib import Path

import numpy as np
import pytest

from navig_audio.enhance import analyze, enhance, plan
from navig_audio.enhance import engine as engine_mod
from navig_audio.enhance.analysis import AnalyzeError, classify
from navig_audio.enhance.engine import Cancelled, EnhanceError
from navig_audio.enhance.verify import verify

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")

SR = 44100


def _wav(path: Path, x: np.ndarray, sr: int = SR) -> Path:
    x = np.atleast_2d(np.asarray(x, dtype=np.float64))
    pcm = (np.clip(x, -1, 1) * 32767).round().astype("<i2").T.copy()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(x.shape[0])
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return path


def _events(dur: float = 3.0, times=(0.3, 0.9, 1.5, 2.1), amp: float = 0.6) -> np.ndarray:
    """Decaying tonal 'ping' events on digital silence — a clean, SFX-like recording."""
    t = np.arange(int(dur * SR)) / SR
    x = np.zeros_like(t)
    for i, t0 in enumerate(times):
        f = (440, 660, 880, 550)[i % 4]
        m = t >= t0
        tt = t[m] - t0
        x[m] += amp * np.sin(2 * np.pi * f * tt) * np.exp(-tt * 6)
    return x


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error", *args], check=True)


# ── analysis + planning ──────────────────────────────────────────────


def test_clean_recording_needs_nothing(tmp_path):
    src = _wav(tmp_path / "clean.wav", _events())
    r = analyze(src)
    assert [f.code for f in r.findings if f.severity != "info"] == []
    assert len(r.onsets_s) == 4
    p = plan(r, "preserve")
    assert p.empty and any("best version" in n for n in p.notes)


def test_noise_between_events_is_found_and_planned(tmp_path):
    rng = np.random.default_rng(1)
    x = _events() + rng.normal(0, 0.004, int(3.0 * SR))
    r = analyze(_wav(tmp_path / "noisy.wav", x))
    f = r.finding("noise")
    assert f is not None and f.severity in ("minor", "major"), r.to_dict()
    assert r.gap_flatness is not None and r.gap_flatness > 0.3
    assert "denoise" in [s.name for s in plan(r, "preserve").stages]


def test_tonal_tail_is_not_noise(tmp_path):
    # A long metallic ring between events is content: spectrally peaky, not flat.
    t = np.arange(int(3.0 * SR)) / SR
    ring = 0.02 * np.sin(2 * np.pi * 3100 * t) + 0.015 * np.sin(2 * np.pi * 4700 * t)
    r = analyze(_wav(tmp_path / "ring.wav", _events() + ring))
    assert r.finding("noise") is None


def test_clipping_dc_hum_and_phase(tmp_path):
    clipped = analyze(_wav(tmp_path / "clip.wav", np.clip(_events(amp=1.8), -1, 1)))
    assert clipped.finding("clipping") is not None
    names = [s.name for s in plan(clipped, "preserve").stages]
    assert "declip" in names and "limit" in names

    dc = analyze(_wav(tmp_path / "dc.wav", _events() * 0.5 + 0.05))
    assert dc.finding("dc_offset") is not None
    assert "dc" in [s.name for s in plan(dc, "preserve").stages]

    t = np.arange(int(3.0 * SR)) / SR
    hum = analyze(_wav(tmp_path / "hum.wav", _events() + 0.03 * np.sin(2 * np.pi * 50 * t)))
    assert hum.hum_hz == 50 and hum.finding("hum") is not None
    stage = next(s for s in plan(hum, "preserve").stages if s.name == "dehum")
    assert "bandreject=f=50" in stage.filter

    x = _events()
    phase = analyze(_wav(tmp_path / "phase.wav", np.vstack([x, -x])))
    assert phase.finding("phase") is not None and phase.stereo_correlation < 0


def test_sfx_never_gets_compression_or_declick(tmp_path):
    r = analyze(_wav(tmp_path / "sfx.wav", _events(dur=1.4, times=(0.1, 0.5, 0.9))))
    assert r.category == "sfx"
    p = plan(r, "studio", {"declick": True})
    names = [s.name for s in p.stages]
    assert "compress" not in names and "declick" not in names
    assert any(s["stage"] == "compress" for s in p.skipped)


def test_cloud_engine_is_opt_in_and_voice_only(tmp_path):
    r = analyze(_wav(tmp_path / "sfx.wav", _events(dur=1.4, times=(0.1, 0.5))))
    p = plan(r, "preserve", {"engine": "elevenlabs", "allow_cloud": True}, engines={"elevenlabs": True})
    assert not p.cloud and any("for voice" in s["why"] for s in p.skipped)
    r.category = "speech"
    p = plan(r, "preserve", {"engine": "elevenlabs"}, engines={"elevenlabs": True})
    assert not p.cloud and any("cloud processing is off" in s["why"] for s in p.skipped)
    p = plan(r, "preserve", {"engine": "elevenlabs", "allow_cloud": True}, engines={"elevenlabs": False})
    assert not p.cloud and any("no ElevenLabs key" in s["why"] for s in p.skipped)
    p = plan(r, "preserve", {"engine": "elevenlabs", "allow_cloud": True}, engines={"elevenlabs": True})
    assert p.cloud and p.stages[0].name == "isolate"


def test_mono_is_never_widened(tmp_path):
    r = analyze(_wav(tmp_path / "m.wav", _events()))
    p = plan(r, "cinematic", {"stereo_width": 1.5})
    assert "width" not in [s.name for s in p.stages]
    assert p.creative


def test_classify_rules():
    assert classify(1.5, 5, {}, 0.0, 18)[0] == "sfx"
    assert classify(30, 120, {"low_mid": 0.5, "high_mid": 0.3}, 0.2, 15)[0] == "speech"
    assert classify(60, 5, {"low_mid": 0.3}, 0.0, 9)[0] == "ambience"


@pytest.mark.parametrize("payload", [b"", b"not audio at all " * 200])
def test_bad_inputs_are_refused(tmp_path, payload):
    f = tmp_path / "bad.mp3"
    f.write_bytes(payload)
    with pytest.raises(AnalyzeError):
        analyze(f)


def test_text_file_has_no_audio_stream(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("hello", encoding="utf-8")
    with pytest.raises(AnalyzeError):
        analyze(f)


def test_size_limit(tmp_path, monkeypatch):
    import navig_audio.enhance.analysis as an

    monkeypatch.setattr(an, "MAX_FILE_BYTES", 1000)
    with pytest.raises(AnalyzeError, match="larger"):
        analyze(_wav(tmp_path / "big.wav", _events()))


def test_low_bitrate_mp3_is_reported_as_lossy(tmp_path):
    rng = np.random.default_rng(2)
    src = _wav(tmp_path / "n.wav", _events() + rng.normal(0, 0.002, int(3.0 * SR)))
    mp3 = tmp_path / "low.mp3"
    _ffmpeg("-i", str(src), "-c:a", "libmp3lame", "-b:a", "32k", str(mp3))
    r = analyze(mp3)
    assert r.lossy and r.finding("lossy_source") is not None


# ── verification catches destroyed audio ─────────────────────────────


@pytest.fixture
def clean_src(tmp_path):
    return _wav(tmp_path / "src.wav", _events())


def _verify(src: Path, out: Path):
    return verify(analyze(src), out, mode="preserve", stages=["limit"], true_peak_db=-1.0)


def test_identical_copy_passes(tmp_path, clean_src):
    out = tmp_path / "same.wav"
    shutil.copy(clean_src, out)
    v = _verify(clean_src, out)
    assert v.verdict == "pass", v.summary


def test_reversed_output_fails(tmp_path, clean_src):
    out = tmp_path / "rev.wav"
    _ffmpeg("-i", str(clean_src), "-af", "areverse", str(out))
    v = _verify(clean_src, out)
    assert v.verdict == "fail" and {"events_kept"} & {c.name for c in v.failed}


def test_truncated_output_fails(tmp_path, clean_src):
    out = tmp_path / "cut.wav"
    _ffmpeg("-i", str(clean_src), "-t", "1.6", str(out))
    v = _verify(clean_src, out)
    assert "duration" in {c.name for c in v.failed}


def test_pitch_shifted_output_fails(tmp_path, clean_src):
    out = tmp_path / "pitch.wav"
    _ffmpeg("-i", str(clean_src), "-af", f"asetrate={int(SR * 1.06)},aresample={SR},atempo=0.9434", str(out))
    v = _verify(clean_src, out)
    assert "pitch" in {c.name for c in v.failed}, [(c.name, c.value) for c in v.checks]


def test_injected_event_fails(tmp_path, clean_src):
    x = _events()
    t = np.arange(x.size) / SR
    m = (t >= 2.6) & (t < 2.7)
    x[m] += 0.5 * np.sin(2 * np.pi * 1200 * (t[m] - 2.6)) * np.exp(-(t[m] - 2.6) * 30)
    out = _wav(tmp_path / "extra.wav", x)
    v = _verify(clean_src, out)
    assert "no_new_events" in {c.name for c in v.failed}


def test_new_content_in_empty_bands_fails(tmp_path):
    t = np.arange(int(3.0 * SR)) / SR
    low = _events() * 0.5  # all energy under ~1 kHz
    src = _wav(tmp_path / "low.wav", low)
    hiss = low + 0.05 * np.sin(2 * np.pi * 12000 * t) * (np.abs(low) > 0.01)
    out = _wav(tmp_path / "hi.wav", hiss)
    v = _verify(src, out)
    assert "no_new_content" in {c.name for c in v.failed} or "tone" in {c.name for c in v.failed}


# ── the engine ───────────────────────────────────────────────────────


def test_clean_file_is_left_alone(tmp_path, clean_src):
    r = enhance(clean_src, mode="preserve", out_dir=tmp_path / "out")
    assert r.verdict == "clean" and r.outputs == [] and r.plan.empty


def test_noisy_file_is_cleaned_verified_and_original_untouched(tmp_path):
    rng = np.random.default_rng(3)
    src = _wav(tmp_path / "noisy.wav", _events() + rng.normal(0, 0.004, int(3.0 * SR)))
    before = _sha(src)
    r = enhance(src, mode="preserve", out_dir=tmp_path / "out", formats=["wav", "flac"])
    assert r.verdict == "pass", r.verification.summary
    assert _sha(src) == before
    assert [Path(o).suffix for o in r.outputs] == [".wav", ".flac"]
    after = analyze(r.outputs[0])
    assert after.noise_floor_db is None or after.noise_floor_db < r.report.noise_floor_db - 3
    assert Path(r.sidecar).is_file()


def test_outputs_never_overwrite_and_repeat_runs_are_stable(tmp_path):
    src = _wav(tmp_path / "c.wav", np.clip(_events(amp=1.8), -1, 1))
    a = enhance(src, mode="preserve", out_dir=tmp_path)
    b = enhance(src, mode="preserve", out_dir=tmp_path)
    assert a.outputs[0] != b.outputs[0] and "take2" in b.outputs[0]
    # Re-processing the result finds nothing left to fix (no drift on repeated passes).
    again = enhance(a.outputs[0], mode="preserve", out_dir=tmp_path / "again")
    assert again.verdict == "clean", [f.code for f in again.report.findings]


def test_silence_is_refused(tmp_path):
    src = _wav(tmp_path / "silent.wav", np.zeros(SR))
    with pytest.raises(EnhanceError, match="silent"):
        enhance(src, mode="studio", out_dir=tmp_path)


def test_failed_verification_retries_safer_then_rejects(tmp_path, monkeypatch):
    # Two defects → two stages (dc + denoise), so a safer plan exists after dropping one.
    rng = np.random.default_rng(4)
    src = _wav(tmp_path / "n.wav", _events() * 0.5 + rng.normal(0, 0.004, int(3.0 * SR)) + 0.05)
    real = engine_mod.verify
    calls = []

    def flaky(report, out, **kw):
        calls.append(list(kw["stages"]))
        v = real(report, out, **kw)
        if "denoise" in kw["stages"]:
            v.verdict, v.summary = "fail", "forced"
        return v

    monkeypatch.setattr(engine_mod, "verify", flaky)
    r = enhance(src, mode="preserve", out_dir=tmp_path / "o")
    assert "denoise" in calls[0] and "denoise" not in calls[-1]
    assert r.verdict in ("pass", "warn") and len(r.attempts) >= 2

    monkeypatch.setattr(engine_mod, "verify", lambda report, out, **kw: _fail(real(report, out, **kw)))
    rej = enhance(src, mode="preserve", out_dir=tmp_path / "rej")
    assert rej.verdict == "fail" and rej.outputs == []
    assert "rejected" in Path(rej.sidecar).name
    assert not list((tmp_path / "rej").glob("*.enhanced*"))


def _fail(v):
    v.verdict = "fail"
    return v


def test_cancel_keeps_nothing(tmp_path):
    rng = np.random.default_rng(5)
    src = _wav(tmp_path / "n.wav", _events() + rng.normal(0, 0.004, int(3.0 * SR)))
    ev = threading.Event()
    ev.set()
    with pytest.raises(Cancelled):
        enhance(src, mode="preserve", out_dir=tmp_path / "o", cancel=ev)
    assert not list((tmp_path / "o").glob("*")) if (tmp_path / "o").exists() else True


def test_concurrent_jobs_get_distinct_outputs(tmp_path):
    src = _wav(tmp_path / "c.wav", np.clip(_events(amp=1.8), -1, 1))
    results, errors = [], []

    def run():
        try:
            results.append(enhance(src, mode="preserve", out_dir=tmp_path / "out"))
        except Exception as exc:  # noqa: BLE001 — surfaced by the assert below
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    outs = [r.outputs[0] for r in results]
    assert len(set(outs)) == 2


def test_provider_failure_keeps_nothing(tmp_path, monkeypatch):
    src = _wav(tmp_path / "s.wav", _events(dur=6, times=(0.3, 1.0, 1.8, 2.6, 3.4, 4.2)))
    monkeypatch.setattr(engine_mod, "available_engines", lambda: {"ffmpeg": True, "elevenlabs": True})

    def boom(_src, _dst):
        raise EnhanceError("ElevenLabs answered 500")

    monkeypatch.setattr(engine_mod, "_isolate", boom)
    with pytest.raises(EnhanceError, match="500"):
        enhance(src, mode="preserve", category="speech", out_dir=tmp_path / "o",
                params={"engine": "elevenlabs", "allow_cloud": True})
    assert not (tmp_path / "o").exists() or not list((tmp_path / "o").glob("*.enhanced*"))


def test_unknown_format_is_refused(tmp_path, clean_src):
    with pytest.raises(EnhanceError, match="format"):
        enhance(clean_src, formats=["ogg"], out_dir=tmp_path)


def test_dry_run_writes_nothing(tmp_path):
    src = _wav(tmp_path / "c.wav", np.clip(_events(amp=1.8), -1, 1))
    r = enhance(src, mode="studio", out_dir=tmp_path / "o", dry_run=True)
    assert r.verdict == "planned" and not r.plan.empty and not (tmp_path / "o").exists()


# ── the CLI ──────────────────────────────────────────────────────────


def test_cli_analyze_dry_run_and_run(tmp_path):
    import json

    from typer.testing import CliRunner

    from navig_audio.commands.audio import audio_app

    src = _wav(tmp_path / "c.wav", np.clip(_events(amp=1.8), -1, 1))
    runner = CliRunner()
    res = runner.invoke(audio_app, ["enhance", str(src), "--analyze", "--json"])
    assert res.exit_code == 0, res.output
    assert any(f["code"] == "clipping" for f in json.loads(res.output)["findings"])

    res = runner.invoke(audio_app, ["enhance", str(src), "--dry-run", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["verdict"] == "planned" and "declip" in [s["name"] for s in data["plan"]["stages"]]

    out = tmp_path / "o"
    res = runner.invoke(audio_app, ["enhance", str(src), "--out", str(out), "--format", "wav,mp3", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert [Path(p).suffix for p in data["outputs"]] == [".wav", ".mp3"]

    res = runner.invoke(audio_app, ["enhance", str(src), "--preset", "audio-enhance/nope"])
    assert res.exit_code == 2
    res = runner.invoke(audio_app, ["enhance", str(tmp_path / "missing.wav")])
    assert res.exit_code == 2


def test_cli_enhance_presets_lists_the_modes():
    import json

    from typer.testing import CliRunner

    from navig_audio.commands.audio import audio_app

    res = CliRunner().invoke(audio_app, ["enhance-presets", "--json"])
    assert res.exit_code == 0, res.output
    ids = {p["id"] for p in json.loads(res.output)}
    assert "audio-enhance/preserve-original" in ids and len(ids) >= 6


def test_shipped_presets_are_valid_audio_enhance_presets():
    from navig_audio.enhance import presets as presets_mod
    from navig_audio.enhance.planner import MODES

    shipped = presets_mod._shipped()
    assert len(shipped) >= 6  # scan floor: an empty dir must not pass vacuously
    for p in shipped:
        assert p["tool"] == "audio.enhance" and p["params"]["mode"] in MODES
    try:
        from navig.presets.model import preset_from_dict
    except ImportError:  # standalone: the catalog validator is navig's
        return
    for p in shipped:
        preset_from_dict(p, source="plugin")


# ── heavy noise + DeepFilterNet (2026-10-16) ─────────────────────────


def _speechlike(dur: float = 4.0) -> np.ndarray:
    """Vowel-like harmonic bursts at a syllable rate — enough for category=speech tests."""
    t = np.arange(int(dur * SR)) / SR
    f0 = 140 + 20 * np.sin(2 * np.pi * 0.7 * t)
    voice = sum((0.5 / k) * np.sin(2 * np.pi * k * np.cumsum(f0) / SR) for k in range(1, 12))
    syll = (np.sin(2 * np.pi * 3.5 * t) > 0.2).astype(float)
    return 0.4 * voice * syll


def test_heavy_noise_is_detected(tmp_path):
    # ~18 dB SNR: no frame sits 25 dB under the peaks, which used to read as "no noise".
    rng = np.random.default_rng(11)
    r = analyze(_wav(tmp_path / "heavy.wav", _events() + rng.normal(0, 0.03, int(3.0 * SR))))
    f = r.finding("noise")
    assert f is not None and f.severity == "major", (r.snr_db, r.gap_flatness)


def test_deepfilter_is_chosen_only_for_speech_noise(tmp_path):
    rng = np.random.default_rng(12)
    src = _wav(tmp_path / "s.wav", _speechlike() + rng.normal(0, 0.02, int(4.0 * SR)))
    rep = analyze(src, category="speech")
    assert rep.finding("noise") is not None
    with_dfn = plan(rep, "preserve", engines={"deepfilter": True})
    stage = next(s for s in with_dfn.stages if s.name == "denoise")
    assert stage.engine == "deepfilter" and stage.settings["atten_lim_db"] == 18
    forced_local = plan(rep, "preserve", {"engine": "local"}, engines={"deepfilter": True})
    assert next(s for s in forced_local.stages if s.name == "denoise").engine == "ffmpeg"
    missing = plan(rep, "preserve", {"engine": "deepfilter"}, engines={"deepfilter": False})
    assert any(s["stage"] == "deepfilter" for s in missing.skipped)
    rep.category = "sfx"
    assert next(s for s in plan(rep, "preserve", engines={"deepfilter": True}).stages
                if s.name == "denoise").engine == "ffmpeg"


def test_deepfilter_identity_flags_an_unknown_binary(tmp_path):
    fake = tmp_path / "deep-filter.exe"
    fake.write_bytes(b"not the real one")
    assert engine_mod.deepfilter_identity(fake) == "unverified"


@pytest.mark.skipif(engine_mod.deepfilter_exe() is None, reason="deep-filter.exe not installed")
def test_deepfilter_end_to_end_reduces_noise_and_keeps_alignment(tmp_path):
    rng = np.random.default_rng(13)
    src = _wav(tmp_path / "s.wav", _speechlike() + rng.normal(0, 0.02, int(4.0 * SR)))
    r = enhance(src, mode="preserve", category="speech", out_dir=tmp_path / "o")
    assert r.verdict in ("pass", "warn"), r.verification.summary
    assert r.cost["render"]["deepfilter"]["engine"] == "deepfilter"
    hard_fail = [c.name for c in r.verification.checks if c.hard and not c.ok]
    assert hard_fail == []
    assert r.verification.after["noise_floor_db"] < r.report.noise_floor_db - 6
