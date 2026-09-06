"""Local Whisper transcription must be time-bounded.

`_transcribe_whisper_local` awaited `run_in_executor(None, _run)` with NO timeout, so a
stalled model download or a wedged `model.transcribe` hung the caller (and the whole
voice pipeline) forever — while the network providers all honoured a timeout. It now
uses a separate, generous `whisper_local_timeout_seconds`; on timeout the worker thread
is abandoned (Python can't kill a thread) but the caller returns a clean error.

No real Whisper: a fake `whisper` module is injected. The hung fake blocks on an Event
the test releases INSIDE the coroutine (before the loop closes), so the abandoned worker
thread can exit and `asyncio.run`'s executor-shutdown doesn't wait on it — a bare
`time.sleep` would leak a non-daemon thread joined at teardown.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import types
from pathlib import Path

from navig_audio.voice.stt import STT, STTConfig, STTProvider


def _install_fake_whisper(monkeypatch, model) -> None:
    fake = types.ModuleType("whisper")
    fake.load_model = lambda name: model  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "whisper", fake)


def test_whisper_local_times_out(monkeypatch):
    release = threading.Event()

    class _HangingModel:
        def transcribe(self, *a, **kw):
            release.wait(5)  # backstop; normally released inside the coro below
            return {"text": "should have timed out"}

    _install_fake_whisper(monkeypatch, _HangingModel())

    async def _go():
        cfg = STTConfig(
            provider=STTProvider.WHISPER_LOCAL, whisper_local_timeout_seconds=0.15
        )
        result = await STT(config=cfg)._transcribe_whisper_local(Path("x.wav"), "en")
        release.set()  # free the abandoned worker BEFORE the loop's executor shuts down
        return result

    result = asyncio.run(_go())
    assert result.success is False
    assert "timed out" in (result.error or "").lower()


def test_whisper_local_success_still_works(monkeypatch):
    class _FastModel:
        def transcribe(self, *a, **kw):
            return {"text": "hello world", "language": "en"}

    _install_fake_whisper(monkeypatch, _FastModel())
    cfg = STTConfig(provider=STTProvider.WHISPER_LOCAL)
    result = asyncio.run(STT(config=cfg)._transcribe_whisper_local(Path("x.wav"), "en"))
    assert result.success is True
    assert result.text == "hello world"
    assert result.language == "en"


def test_whisper_local_zero_disables_the_bound(monkeypatch):
    """`0` keeps the old unbounded behaviour — a fast call still returns normally."""

    class _FastModel:
        def transcribe(self, *a, **kw):
            return {"text": "ok"}

    _install_fake_whisper(monkeypatch, _FastModel())
    cfg = STTConfig(provider=STTProvider.WHISPER_LOCAL, whisper_local_timeout_seconds=0)
    result = asyncio.run(STT(config=cfg)._transcribe_whisper_local(Path("x.wav"), "en"))
    assert result.success is True and result.text == "ok"
