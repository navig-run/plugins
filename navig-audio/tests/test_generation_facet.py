"""Audio generation facet — registry wiring + the n>1 uniquify behaviour.

No live ElevenLabs calls: ``AudioGenerator`` is monkeypatched with a fake that
writes dummy bytes. Asserts the facet registers an AUDIO backend into core's
generator registry and that a batch (``n>1``) never collides on disk.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from navig_audio.generation import _audio_backend, register_audio_facet


class _FakeAud:
    """Stand-in for navig.tools.audio_generation.GeneratedAudio."""

    def __init__(self, local_path: str) -> None:
        self.local_path = local_path
        self.model = "music_v1"

    def to_dict(self) -> dict:
        return {"local_path": self.local_path, "model": self.model}


class _FakeGen:
    """Fake AudioGenerator: writes a FIXED filename each call (worst-case collision)."""

    def __init__(self, cfg) -> None:
        self.cfg = cfg

    async def generate(self, prompt, kind="music", duration_s=None, **_kw) -> _FakeAud:
        out = Path(self.cfg.output_dir)
        out.mkdir(parents=True, exist_ok=True)
        p = out / f"{kind}_fixedstamp.mp3"
        p.write_bytes(b"ID3fake-audio-bytes")
        return _FakeAud(str(p))

    async def close(self) -> None:  # noqa: D401 - trivial
        return None


def test_backend_honours_n_without_collision(monkeypatch, tmp_path):
    import navig.tools.audio_generation as ag

    monkeypatch.setattr(ag, "AudioGenerator", _FakeGen)
    out = tmp_path / "clips"

    results = asyncio.run(_audio_backend("epic score", kind="music", n=3, out_dir=out))

    assert len(results) == 3
    paths = {r.local_path for r in results}
    assert len(paths) == 3, "n>1 batch must produce distinct files"
    for p in paths:
        assert Path(p).exists()


def test_backend_n1_keeps_original_name(monkeypatch, tmp_path):
    import navig.tools.audio_generation as ag

    monkeypatch.setattr(ag, "AudioGenerator", _FakeGen)
    out = tmp_path / "single"

    results = asyncio.run(_audio_backend("a chime", kind="sfx", n=1, out_dir=out))

    assert len(results) == 1
    # n==1 leaves the underlying client's own filename untouched (no _00 suffix).
    assert Path(results[0].local_path).name == "sfx_fixedstamp.mp3"


def test_backend_drops_a_single_clip_that_wrote_no_file(monkeypatch, tmp_path):
    """Regression: the n==1 path used to skip the file-exists check (only n>1 had it),
    so a single clip the client reported but never wrote came back as a dead path a
    caller would report as generated. It must be dropped, like a batch clip."""
    import navig.tools.audio_generation as ag

    class _NoFileGen:
        def __init__(self, cfg) -> None:
            self.cfg = cfg

        async def generate(self, prompt, kind="music", duration_s=None, **_kw) -> _FakeAud:
            # report a path the client "would" write, but never actually write it
            return _FakeAud(str(Path(self.cfg.output_dir) / "ghost.mp3"))

        async def close(self) -> None:
            return None

    monkeypatch.setattr(ag, "AudioGenerator", _NoFileGen)
    results = asyncio.run(_audio_backend("silence", kind="music", n=1, out_dir=tmp_path))
    assert results == []  # a fileless single clip is dropped, not returned as a dead path


def test_register_audio_facet_wires_registry():
    from navig.media.types import MediaModality, get_generator

    register_audio_facet()
    assert get_generator(MediaModality.AUDIO) is _audio_backend
