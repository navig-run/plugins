"""AI regeneration: consent, operations, labelling and provenance — with a fake provider.

The fake records every call and returns synthetic audio, so the whole flow (describe →
provider → convert → similarity → sidecar) runs without uploading or billing anything.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import shutil
import wave
import zipfile
from pathlib import Path

import numpy as np
import pytest

from navig_audio.regenerate import RegenerateError, regenerate
from navig_audio.regenerate import core as core_mod
from navig_audio.regenerate.description import measured_description

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
SR = 44100


def _wav_bytes(x: np.ndarray, sr: int = SR) -> bytes:
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _pings(dur: float = 1.5, f: float = 880.0, times=(0.1, 0.5, 0.9)) -> np.ndarray:
    t = np.arange(int(dur * SR)) / SR
    x = np.zeros_like(t)
    for t0 in times:
        m = t >= t0
        x[m] += 0.5 * np.sin(2 * np.pi * f * (t[m] - t0)) * np.exp(-(t[m] - t0) * 8)
    return x


class FakeProvider:
    def __init__(self):
        self.calls: list[tuple] = []

    async def change_voice(self, src: Path, voice_id: str) -> bytes:
        self.calls.append(("change_voice", Path(src).name, voice_id))
        return Path(src).read_bytes() if Path(src).suffix == ".wav" else _wav_bytes(_pings(2.0))

    async def sfx(self, prompt: str, duration_s: float) -> bytes:
        self.calls.append(("sfx", prompt, duration_s))
        return _wav_bytes(_pings(duration_s, f=660.0))

    async def music(self, prompt: str, duration_s: float, *, instrumental: bool) -> bytes:
        self.calls.append(("music", prompt, duration_s, instrumental))
        return _wav_bytes(0.2 * np.sin(2 * np.pi * 110 * np.arange(int(duration_s * SR)) / SR))

    async def stems(self, src: Path, *, six: bool) -> bytes:
        self.calls.append(("stems", Path(src).name, six))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            names = ("drums", "bass", "guitar", "piano", "vocals", "other") if six else ("vocals", "instrumental")
            for n in names:
                z.writestr(f"stems/{n}.wav", _wav_bytes(_pings(2.0, f=200 + 50 * len(n))))
        return buf.getvalue()


@pytest.fixture
def src(tmp_path):
    p = tmp_path / "money.wav"
    p.write_bytes(_wav_bytes(_pings()))
    return p


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_nothing_runs_without_consent(src, tmp_path):
    fake = FakeProvider()
    with pytest.raises(RegenerateError, match="allow cloud"):
        regenerate(src, op="variations", allow_cloud=False, provider=fake, out_dir=tmp_path / "o")
    assert fake.calls == []
    with pytest.raises(RegenerateError, match="uploads the file"):
        regenerate(src, op="variations", allow_cloud=False, use_ai_description=True, provider=fake)


def test_dry_run_reports_what_would_be_sent_and_generates_nothing(src, tmp_path):
    fake = FakeProvider()
    r = regenerate(src, op="variations", allow_cloud=False, dry_run=True, count=3, provider=fake,
                   out_dir=tmp_path / "o")
    assert fake.calls == [] and r.takes == []
    assert r.plan["kind"] == "sfx" and r.plan["count"] == 3 and r.plan["uploads_source"] is False
    assert r.plan["seconds_billed"] == pytest.approx(r.plan["duration_s"] * 3, abs=0.02)


def test_variations_send_only_text_and_label_the_output(src, tmp_path):
    fake = FakeProvider()
    before = _sha(src)
    r = regenerate(src, op="variations", allow_cloud=True, direction="heavier, 8-bit arcade", count=2,
                   provider=fake, out_dir=tmp_path / "o")
    assert [c[0] for c in fake.calls] == ["sfx", "sfx"]
    prompt = fake.calls[0][1]
    assert "heavier, 8-bit arcade" in prompt and "sound effect" in prompt
    assert len(r.takes) == 2 and all(Path(t.path).name.startswith("money.regen-var") for t in r.takes)
    assert _sha(src) == before
    side = json.loads(Path(r.sidecar).read_text(encoding="utf-8"))
    assert side["generated"] is True and "GENERATED" in side["note"]
    assert "reading" in r.takes[0].similarity


def test_voice_needs_a_voice_and_speech(src):
    with pytest.raises(RegenerateError, match="choose a voice"):
        regenerate(src, op="voice", allow_cloud=True, provider=FakeProvider())
    with pytest.raises(RegenerateError, match="works on speech"):
        regenerate(src, op="voice", voice_id="v1", allow_cloud=True, provider=FakeProvider())


def test_voice_change_on_speech(src, tmp_path):
    fake = FakeProvider()
    r = regenerate(src, op="voice", voice_id="v1", category="speech", allow_cloud=True, provider=fake,
                   out_dir=tmp_path / "o")
    assert fake.calls == [("change_voice", "money.wav", "v1")]
    assert r.plan["uploads_source"] is True
    assert r.takes[0].similarity["reading"].startswith("follows")  # fake returns the same audio


def test_replace_a_stem_generates_and_remixes(src, tmp_path):
    fake = FakeProvider()
    r = regenerate(src, op="replace", stem="drums", replace_with="lo-fi brushed kit", allow_cloud=True,
                   provider=fake, out_dir=tmp_path / "o")
    kinds = [c[0] for c in fake.calls]
    assert kinds == ["stems", "music"] and fake.calls[0][2] is True  # six-stem split
    assert "isolated drums track only: lo-fi brushed kit" in fake.calls[1][1]
    assert Path(r.takes[0].path).name.startswith("money.regen-replace-drums")
    assert set(r.plan["stems"]) >= {"drums", "bass", "vocals"}
    assert "stem_gain_db" in r.plan


def test_replace_vocals_re_voices_the_vocal_stem(src, tmp_path):
    fake = FakeProvider()
    r = regenerate(src, op="replace", stem="vocals", voice_id="v2", allow_cloud=True, provider=fake,
                   out_dir=tmp_path / "o")
    assert [c[0] for c in fake.calls] == ["stems", "change_voice"] and fake.calls[0][2] is False
    assert fake.calls[1] == ("change_voice", "vocals.wav", "v2")
    assert r.takes


def test_bad_requests_are_refused_before_any_call(src):
    fake = FakeProvider()
    for kw in ({"op": "remix"}, {"op": "replace", "stem": "kazoo"}, {"op": "replace", "stem": "drums"},
               {"op": "replace", "stem": "vocals"}):
        with pytest.raises(RegenerateError):
            regenerate(src, allow_cloud=True, provider=fake, **kw)
    assert fake.calls == []


def test_provider_returning_nothing_is_an_error_and_leaves_no_file(src, tmp_path):
    class Empty(FakeProvider):
        async def sfx(self, prompt, duration_s):
            return b""

    with pytest.raises(RegenerateError, match="no audio"):
        regenerate(src, op="variations", allow_cloud=True, count=1, provider=Empty(), out_dir=tmp_path / "o")
    assert not list((tmp_path / "o").glob("*.regen-*"))


def test_measured_description_is_local_and_plain(src):
    from navig_audio.enhance.analysis import analyze

    d = measured_description(analyze(src))
    assert "sound effect" in d and "3 distinct hits" in d


def test_ai_description_goes_through_the_sdk_with_an_audio_part(src, monkeypatch):
    import navig_audio.regenerate.description as describe_mod

    seen = {}

    def fake_generate(messages, **kw):
        seen["messages"], seen["kw"] = messages, kw
        return "  coins clink twice,   then a bell rings  "

    import navig_sdk.ai as ai

    monkeypatch.setattr(ai, "generate", fake_generate)
    text = describe_mod.ai_description(src)
    assert text == "coins clink twice, then a bell rings"
    parts = seen["messages"][0]["content"]
    assert parts[1]["type"] == "input_audio" and parts[1]["input_audio"]["format"] == "wav"
    assert seen["kw"]["provider"] == "openrouter"


def test_cli_dry_run_and_consent(src):
    from typer.testing import CliRunner

    from navig_audio.commands.audio import audio_app

    runner = CliRunner()
    res = runner.invoke(audio_app, ["regenerate", str(src), "--op", "variations", "--dry-run", "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["plan"]["uploads_source"] is False
    res = runner.invoke(audio_app, ["regenerate", str(src), "--op", "variations"])
    assert res.exit_code == 2 and "allow" in res.output.lower()


def test_run_refuses_inside_an_event_loop():
    async def inner():
        with pytest.raises(RegenerateError, match="worker thread"):
            core_mod._run(asyncio.sleep(0))

    asyncio.run(inner())
