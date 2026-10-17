"""`navig voice usage | policy | budget` and `speak --profile`."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from navig_audio.commands import voice as cmd
from navig_audio.voice import usage

runner = CliRunner()


class _CM:
    def __init__(self):
        self.data: dict = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set_global(self, key, value):
        self.data[key] = value


@pytest.fixture
def cm(monkeypatch, tmp_path):
    fake = _CM()
    monkeypatch.setattr(cmd, "_cm", lambda: fake)
    monkeypatch.setattr(usage, "ledger_path", lambda: tmp_path / "voice" / "usage.json")
    monkeypatch.setenv("COLUMNS", "120")
    return fake


def test_policy_shows_and_sets(cm):
    res = runner.invoke(cmd.voice_app, ["policy"])
    assert res.exit_code == 0 and "auto" in res.output
    res = runner.invoke(cmd.voice_app, ["policy", "free", "--paid-fallback"])
    assert res.exit_code == 0
    assert cm.data == {"voice.policy": "free", "voice.paid_fallback": True}
    res = runner.invoke(cmd.voice_app, ["policy", "loud"])
    assert res.exit_code == 2 and "voice.policy" not in {k for k, v in cm.data.items() if v == "loud"}


def test_budget_sets_and_zero_means_off(cm):
    res = runner.invoke(cmd.voice_app, ["budget", "0"])
    assert res.exit_code == 0 and "off" in res.output
    assert cm.data["voice.daily_char_budget"] == 0
    res = runner.invoke(cmd.voice_app, ["budget", "5000"])
    assert "5,000" in res.output


def test_usage_lists_paid_days_and_json(cm):
    usage.record("elevenlabs", 1200, usd=0.096)
    res = runner.invoke(cmd.voice_app, ["usage"])
    assert res.exit_code == 0 and "1,200" in res.output and "elevenlabs" in res.output
    res = runner.invoke(cmd.voice_app, ["usage", "--json"])
    data = json.loads(res.output)
    assert data["paid_chars_today"] == 1200 and data["policy"] == "auto"


def test_speak_with_an_unknown_profile_fails_cleanly(monkeypatch):
    from navig.presets import catalog

    monkeypatch.setattr(catalog, "get_preset", lambda pid: None)
    res = runner.invoke(cmd.voice_app, ["speak", "hi", "--profile", "speech/nope", "--no-play"])
    assert res.exit_code == 2 and "no voice profile" in res.output


def test_speak_with_a_profile_uses_its_settings(monkeypatch, tmp_path):
    from navig.presets import catalog

    from navig_audio.voice.tts import TTS, TTSResult

    class _P:
        name, tool = "Deep RU", "speech"
        params = {"provider": "edge", "voice": "ru-RU-DmitryNeural", "lang": "ru", "pitch_hz": -6, "pronounce": ["NAVIG=Навиг"]}

    monkeypatch.setattr(catalog, "get_preset", lambda pid: _P())
    seen = {}

    async def fake(self, text, provider=None, voice=None, output_path=None, **kw):
        seen.update(text=text, provider=provider, voice=voice, pitch=self.config.pitch, **kw)
        out = tmp_path / "o.mp3"
        out.write_bytes(b"ID3")
        return TTSResult(success=True, audio_path=out, provider=provider, voice=voice)

    monkeypatch.setattr(TTS, "synthesize", fake)
    res = runner.invoke(cmd.voice_app, ["speak", "Это NAVIG", "--profile", "speech/x", "--no-play"])
    assert res.exit_code == 0, res.output
    assert seen["text"] == "Это Навиг" and seen["voice"] == "ru-RU-DmitryNeural"
    assert seen["pitch"] == "-6Hz" and seen["lang"] == "ru"
