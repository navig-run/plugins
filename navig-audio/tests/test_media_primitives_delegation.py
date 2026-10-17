"""Audio's loudness meter, SRT stamps and durations are navig-media-core's, contracts kept."""

from __future__ import annotations

import pytest

from navig_media_core.media import audio_edit


def test_mastering_loudness_is_the_shared_meter() -> None:
    import navig_media_core.media.loudness as shared
    from navig_audio.mastering import loudness

    assert loudness is shared
    assert loudness.LoudnessError is shared.LoudnessError


def test_srt_format_timestamp_carries() -> None:
    from navig_audio.podcast.srt import format_timestamp

    assert format_timestamp(1.9996) == "00:00:02,000"
    assert format_timestamp(-1) == "00:00:00,000"


def _boom(*_a, **_k):
    raise audio_edit.AudioEditError("unreadable")


def test_master_duration_is_zero_when_unreadable(monkeypatch) -> None:
    from navig_audio.mastering import master

    monkeypatch.setattr(audio_edit, "probe_duration", _boom)
    assert master._duration("x.wav") == 0.0


def test_voice_line_duration_raises_line_error(monkeypatch) -> None:
    from navig_audio.voice import line

    monkeypatch.setattr(audio_edit, "probe_duration", _boom)
    with pytest.raises(line.LineError, match="no duration"):
        line.duration("x.mp3")
