"""Pipeline's duration / loudness / SRT helpers delegate to navig-media-core, keeping their contracts."""

from __future__ import annotations

import pytest

from navig_media_core.media import audio_edit, loudness
from navig_pipeline import captions, episode, series


def _boom(*_a, **_k):
    raise audio_edit.AudioEditError("unreadable")


def test_episode_duration_raises_episode_error(monkeypatch) -> None:
    monkeypatch.setattr(audio_edit, "probe_duration", _boom)
    with pytest.raises(episode.EpisodeError, match="unreadable"):
        episode._duration("x.mp3")


def test_series_duration_is_none_when_unreadable(monkeypatch) -> None:
    monkeypatch.setattr(audio_edit, "probe_duration", _boom)
    assert series._duration("x.mp4") is None


def test_series_duration_rounds_to_centiseconds(monkeypatch) -> None:
    monkeypatch.setattr(audio_edit, "probe_duration", lambda *_a, **_k: 12.34567)
    assert series._duration("x.mp4") == 12.35


def test_episode_loudness_is_minus_70_for_silence_and_failure(monkeypatch) -> None:
    def silent(*_a, **_k):
        return loudness.Loudness(path="x", lufs=float("-inf"), lra=0.0, true_peak=float("-inf"),
                                 threshold=float("-inf"))

    monkeypatch.setattr(loudness, "measure", silent)
    assert episode.loudness("x.mp3", 0.0, 1.0) == -70.0

    def fail(*_a, **_k):
        raise loudness.LoudnessError("nope")

    monkeypatch.setattr(loudness, "measure", fail)
    assert episode.loudness("x.mp3", 0.0, 1.0) == -70.0


def test_episode_loudness_passes_the_window(monkeypatch) -> None:
    seen = {}

    def fake(path, **k):
        seen.update(k)
        return loudness.Loudness(path=str(path), lufs=-16.5, lra=1.0, true_peak=-1.0, threshold=-26.0)

    monkeypatch.setattr(loudness, "measure", fake)
    assert episode.loudness("x.mp3", 3.0, 2.5) == -16.5
    assert seen == {"start": 3.0, "duration": 2.5}


def test_srt_never_prints_a_four_digit_millisecond_field() -> None:
    line = captions.Line(text="hi", start=0.5, end=1.9996)
    out = captions.to_srt([line])
    assert "00:00:02,000" in out and ",1000" not in out
