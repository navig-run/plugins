"""navig-video's loudness reading is navig-media-core's ebur128 meter (same parse, bounded)."""

from __future__ import annotations

from navig_media_core.media import loudness
from navig_video import parity


def test_parity_loudness_delegates_to_the_shared_meter(monkeypatch) -> None:
    seen = []
    monkeypatch.setattr(loudness, "integrated_lufs", lambda p, **_k: seen.append(p) or -14.2)
    assert parity._loudness("a.mp4") == -14.2
    assert seen == ["a.mp4"]


def test_parity_loudness_is_none_when_unmeasurable(monkeypatch) -> None:
    monkeypatch.setattr(loudness, "integrated_lufs", lambda *_a, **_k: None)
    assert parity._loudness("a.mp4") is None
