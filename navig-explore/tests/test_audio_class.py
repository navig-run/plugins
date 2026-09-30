"""Feature extraction + the tiny logistic-regression trainer.

No audio files on disk: features are computed from synthesised waveforms, so the tests
run anywhere and assert the properties the classifier actually leans on.
"""
from __future__ import annotations

import numpy as np
import pytest

from navig_explore import audio_class as ac


def _tone(seconds=4.0, f0=220.0, sr=ac.SR):
    """A steady harmonic tone — the caricature of 'music': pitched and sustained."""
    t = np.arange(int(seconds * sr)) / sr
    x = sum(np.sin(2 * np.pi * f0 * k * t) / k for k in (1, 2, 3))
    return (x / np.max(np.abs(x))).astype(np.float32)


def _noise(seconds=4.0, sr=ac.SR, seed=0):
    """White noise — the caricature of 'sfx': broadband and pitchless."""
    rng = np.random.default_rng(seed)
    return rng.normal(0, 0.3, int(seconds * sr)).astype(np.float32)


def _pulsed(seconds=4.0, sr=ac.SR, rate=4.0):
    """A tone gated at 4 Hz — the caricature of speech's syllabic envelope."""
    x = _tone(seconds, 150.0, sr)
    t = np.arange(x.size) / sr
    gate = (np.sin(2 * np.pi * rate * t) > 0).astype(np.float32)
    return (x * gate).astype(np.float32)


def test_features_have_every_declared_key():
    f = ac.features_from_array(_tone(), dur=4.0)
    assert f is not None
    missing = [k for k in ac.FEATURES if k not in f]
    assert not missing, f"FEATURES declares keys the extractor never emits: {missing}"


def test_vector_matches_feature_order_and_is_finite():
    f = ac.features_from_array(_noise(), dur=4.0)
    v = ac.vec(f)
    assert v.shape == (len(ac.FEATURES),)
    assert np.isfinite(v).all()


def test_tone_is_more_harmonic_and_less_flat_than_noise():
    tone = ac.features_from_array(_tone(), dur=4.0)
    noise = ac.features_from_array(_noise(), dur=4.0)
    assert tone["harmonicity_mean"] > noise["harmonicity_mean"]
    assert tone["flatness_mean"] < noise["flatness_mean"]   # tonal vs broadband


def test_pulsed_signal_shows_syllabic_modulation_and_pauses():
    steady = ac.features_from_array(_tone(), dur=4.0)
    pulsed = ac.features_from_array(_pulsed(), dur=4.0)
    assert pulsed["mod4hz_ratio"] > steady["mod4hz_ratio"]
    assert pulsed["low_energy_ratio"] > steady["low_energy_ratio"]


def test_features_are_loudness_invariant():
    """Peak normalisation means a quiet rip and a loud master must score the same."""
    x = _tone()
    loud = ac.features_from_array(x, dur=4.0)
    quiet = ac.features_from_array((x * 0.05).astype(np.float32), dur=4.0)
    # decode_window normalises; features_from_array gets pre-normalised input in the
    # real pipeline, so normalise here the same way before comparing.
    quiet_norm = ac.features_from_array((x * 0.05 / np.max(np.abs(x * 0.05))).astype(np.float32),
                                        dur=4.0)
    assert quiet_norm["centroid_mean"] == pytest.approx(loud["centroid_mean"], rel=1e-6)
    assert quiet["dyn_range_db"] == pytest.approx(loud["dyn_range_db"], abs=1.0)


def test_duration_is_reported_but_never_a_model_feature():
    """A 4-minute album track and a 20 s clip must not be separable by length alone."""
    assert "dur" not in ac.FEATURES
    f = ac.features_from_array(_tone(), dur=240.0)
    assert f["dur"] == 240.0


def _samples():
    out = []
    for i in range(24):
        out.append((ac.features_from_array(_tone(4.0, 180 + 7 * i), 4.0), "music"))
        out.append((ac.features_from_array(_noise(4.0, seed=i), 4.0), "sfx"))
        out.append((ac.features_from_array(_pulsed(4.0, rate=3.5 + 0.05 * i), 4.0), "voice"))
    return out


def test_train_separates_three_synthetic_classes():
    m = ac.train(_samples(), folds=4)
    assert m["labels"] == ["music", "sfx", "voice"]
    assert m["cv_accuracy"] > 0.9, m["confusion"]
    assert m["n_train"] == 72


def test_predict_returns_calibrated_probabilities_per_label():
    m = ac.train(_samples(), folds=4)
    got = ac.predict(m, [ac.features_from_array(_noise(4.0, seed=99), 4.0)])
    label, conf, probs = got[0]
    assert label == "sfx"
    assert 0.0 <= conf <= 1.0
    assert set(probs) == {"music", "sfx", "voice"}
    assert sum(probs.values()) == pytest.approx(1.0, abs=1e-6)


def test_predict_refuses_a_model_trained_on_other_features():
    m = ac.train(_samples(), folds=4)
    m["features"] = m["features"][:-1]
    with pytest.raises(ValueError, match="feature set"):
        ac.predict(m, [ac.features_from_array(_tone(), 4.0)])


def test_predict_refuses_a_vad_model_when_vad_is_unavailable(monkeypatch):
    """Scoring three zeroed features would look fine and be wrong — it must raise."""
    m = ac.train(_samples(), folds=4)
    m["vad"] = True
    monkeypatch.setattr(ac, "vad_available", lambda: False)
    with pytest.raises(ValueError, match="VAD"):
        ac.predict(m, [ac.features_from_array(_tone(), 4.0)])


def test_speech_features_degrade_to_zero_without_faster_whisper(monkeypatch):
    monkeypatch.setattr(ac, "vad_available", lambda: False)
    s = ac.speech_features(_pulsed())
    assert s == {"speech_ratio": 0.0, "speech_seg_per_min": 0.0, "speech_seg_mean_s": 0.0}


def test_iter_audio_skips_sidecar_and_trash_dirs(tmp_path):
    (tmp_path / "keep.mp3").write_bytes(b"x")
    (tmp_path / "note.txt").write_bytes(b"x")
    for skipped in (".mediaexplorer", "_review", ".trash"):
        d = tmp_path / skipped
        d.mkdir()
        (d / "hidden.mp3").write_bytes(b"x")
    found = {p.name for p in ac.iter_audio(tmp_path)}
    assert found == {"keep.mp3"}


def test_iter_audio_includes_extensionless_dumps_by_default(tmp_path):
    """Telegram writes 'files/81' with no suffix; skipping those loses the folder."""
    (tmp_path / "keep.mp3").write_bytes(b"x")
    (tmp_path / "81").write_bytes(b"x")
    (tmp_path / "note.txt").write_bytes(b"x")
    assert {p.name for p in ac.iter_audio(tmp_path)} == {"keep.mp3", "81"}
    assert {p.name for p in ac.iter_audio(tmp_path, extensionless=False)} == {"keep.mp3"}


def test_iter_audio_skips_other_tools_quarantine_folders(tmp_path):
    """A sort must never undo a previous pass's dedupe by promoting its `_dupes`."""
    (tmp_path / "keep.mp3").write_bytes(b"x")
    for quarantine in ("_dupes", "_duplicates", "_quarantine"):
        d = tmp_path / quarantine
        d.mkdir()
        (d / "set-aside.mp3").write_bytes(b"x")
    assert {p.name for p in ac.iter_audio(tmp_path)} == {"keep.mp3"}
