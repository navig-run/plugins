"""The ElevenLabs seams in navig-audio: `audio isolate` and the Scribe STT branch.

No live calls anywhere — ``AudioGenerator`` is monkeypatched at the same module seam
``test_gen_command`` uses. What is pinned: the cleaned bytes actually reach disk, an
EMPTY provider result is refused (a 0-byte "success" would be silent corruption), the
Scribe branch turns a payload into a proper ``STTResult`` with word timings, "" stays
detect (never a pinned language), and a provider failure is a RESULT — never a crash
out of the dispatch loop.
"""

from __future__ import annotations

from typer.testing import CliRunner

from navig_audio.commands.audio import audio_app
from navig_audio.voice.stt import STT, STTConfig, STTProvider

runner = CliRunner()


class _IsolateGen:
    """AudioGenerator stand-in for the isolate command."""

    payload = b"cleaned-audio"

    def __init__(self, cfg=None) -> None:
        pass

    async def isolate_voice(self, src):
        return self.payload

    async def close(self) -> None:
        pass


def test_isolate_writes_the_cleaned_file_next_to_the_source(monkeypatch, tmp_path):
    import navig_generate.tools.audio_generation as ag

    monkeypatch.setattr(ag, "AudioGenerator", _IsolateGen)
    src = tmp_path / "raw.mp3"
    src.write_bytes(b"noisy")

    res = runner.invoke(audio_app, ["isolate", str(src)])
    assert res.exit_code == 0, res.output
    out = tmp_path / "raw-isolated.mp3"
    assert out.exists()
    assert out.read_bytes() == b"cleaned-audio"
    assert "Isolated" in res.output


def test_isolate_refuses_an_empty_provider_result(monkeypatch, tmp_path):
    """A 200-with-empty-body must fail loudly, not write a 0-byte 'clean' file."""
    import navig_generate.tools.audio_generation as ag

    class _EmptyGen(_IsolateGen):
        payload = b""

    monkeypatch.setattr(ag, "AudioGenerator", _EmptyGen)
    src = tmp_path / "raw.mp3"
    src.write_bytes(b"noisy")

    res = runner.invoke(audio_app, ["isolate", str(src)])
    assert res.exit_code == 1, res.output
    assert not (tmp_path / "raw-isolated.mp3").exists()


def test_isolate_json_output_carries_path_and_source(monkeypatch, tmp_path):
    import json as _json

    import navig_generate.tools.audio_generation as ag

    monkeypatch.setattr(ag, "AudioGenerator", _IsolateGen)
    src = tmp_path / "raw.mp3"
    src.write_bytes(b"noisy")

    res = runner.invoke(audio_app, ["isolate", str(src), "--json"])
    assert res.exit_code == 0, res.output
    payload = _json.loads(res.output)
    assert payload["op"] == "isolate"
    assert payload["path"].endswith("raw-isolated.mp3")


# ── Scribe (ElevenLabs STT branch) ────────────────────────────────────────────


class _ScribeGen:
    """AudioGenerator stand-in for the Scribe branch."""

    payload: dict = {
        "text": "hello world",
        "language_code": "eng",
        "words": [
            {"text": "hello", "start": 0.0, "end": 0.4},
            {"text": "world", "start": 0.5, "end": 0.9},
        ],
    }
    captured: dict = {}

    def __init__(self, cfg=None) -> None:
        pass

    async def transcribe(self, path, **kwargs):
        type(self).captured = dict(kwargs)
        return self.payload

    async def close(self) -> None:
        pass


async def test_scribe_branch_returns_text_with_word_timings(monkeypatch, tmp_path):
    import navig_generate.tools.audio_generation as ag

    monkeypatch.setattr(ag, "AudioGenerator", _ScribeGen)
    _ScribeGen.captured = {}

    res = await STT(config=STTConfig())._transcribe_elevenlabs(tmp_path / "x.wav", "")
    assert res.success, res.error
    assert res.text == "hello world"
    assert res.provider is STTProvider.ELEVENLABS
    assert res.language == "eng"
    assert res.segments == [
        {"word": "hello", "start": 0.0, "end": 0.4},
        {"word": "world", "start": 0.5, "end": 0.9},
    ]
    # "" must reach the API as NO language — this module's detect rule.
    assert _ScribeGen.captured.get("language_code") is None


async def test_a_pinned_language_reaches_scribe(monkeypatch, tmp_path):
    import navig_generate.tools.audio_generation as ag

    monkeypatch.setattr(ag, "AudioGenerator", _ScribeGen)
    _ScribeGen.captured = {}

    await STT(config=STTConfig())._transcribe_elevenlabs(tmp_path / "x.wav", "fr")
    assert _ScribeGen.captured.get("language_code") == "fr"


async def test_a_scribe_failure_is_a_result_not_a_crash(monkeypatch, tmp_path):
    import navig_generate.tools.audio_generation as ag

    class _BrokenGen(_ScribeGen):
        async def transcribe(self, path, **kwargs):
            raise RuntimeError("402 payment_required")

    monkeypatch.setattr(ag, "AudioGenerator", _BrokenGen)

    res = await STT(config=STTConfig())._transcribe_elevenlabs(tmp_path / "x.wav", "")
    assert res.success is False
    assert "402" in (res.error or "")
    assert res.provider is STTProvider.ELEVENLABS


async def test_dispatch_routes_the_elevenlabs_provider(monkeypatch, tmp_path):
    seen = {}

    async def _spy(self, path, language, **kwargs):
        seen["language"] = language
        from navig_audio.voice.stt import STTResult

        return STTResult(success=True, text="ok")

    monkeypatch.setattr(STT, "_transcribe_elevenlabs", _spy)
    res = await STT(config=STTConfig())._transcribe_with_provider(
        tmp_path / "x.wav", STTProvider.ELEVENLABS, "de"
    )
    assert res.success
    assert seen["language"] == "de"
