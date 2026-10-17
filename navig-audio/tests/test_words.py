"""Word-level transcription (captions) — both engines return Whisper's verbose_json shape."""

from __future__ import annotations

import sys
import types

import pytest

from navig_audio.voice import words as w


@pytest.fixture
def clip(tmp_path):
    p = tmp_path / "clip.mp3"
    p.write_bytes(b"\x00" * 128)
    return p


def _fake_faster_whisper(monkeypatch, *, seen: dict) -> None:
    class _Model:
        def __init__(self, name, device="auto", **kw):
            seen.setdefault("devices", []).append(device)
            seen["model"] = name

        def transcribe(self, path, language=None, word_timestamps=False, **kw):
            seen["language"], seen["word_timestamps"] = language, word_timestamps
            word = types.SimpleNamespace
            segs = [
                types.SimpleNamespace(
                    text=" hello there ", start=0.0, end=1.0,
                    words=[word(word=" hello", start=0.0, end=0.4), word(word=" there", start=0.5, end=1.0)],
                ),
            ]
            return iter(segs), types.SimpleNamespace(language="en", duration=1.0)

    fake = types.ModuleType("faster_whisper")
    fake.WhisperModel = _Model  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)


def test_local_engine_returns_word_timings(monkeypatch, clip):
    seen: dict = {}
    _fake_faster_whisper(monkeypatch, seen=seen)
    out = w.transcribe_words(clip, engine="whisper_local", language="fr", model="tiny")
    assert out["words"] == [
        {"word": "hello", "start": 0.0, "end": 0.4},
        {"word": "there", "start": 0.5, "end": 1.0},
    ]
    assert out["segments"] == [{"text": "hello there", "start": 0.0, "end": 1.0}]
    assert (out["text"], out["language"], out["duration"]) == ("hello there", "en", 1.0)
    assert seen["word_timestamps"] is True and seen["language"] == "fr" and seen["model"] == "tiny"


def test_no_key_picks_the_local_engine(monkeypatch, clip):
    seen: dict = {}
    _fake_faster_whisper(monkeypatch, seen=seen)
    monkeypatch.setattr(w, "openai_key", lambda: None)
    assert w.transcribe_words(clip)["words"], "expected the local engine to answer"
    assert seen["devices"] == ["auto"]


def test_a_key_picks_the_api_with_word_granularity(monkeypatch, clip):
    import httpx

    sent = {}

    def _post(url, headers=None, data=None, files=None, timeout=None):
        sent.update(url=url, data=data, auth=headers["Authorization"])
        return httpx.Response(200, json={"text": "hi", "words": [{"word": "hi", "start": 0, "end": 1}]})

    monkeypatch.setattr(w, "openai_key", lambda: "sk-test")
    monkeypatch.setattr(httpx, "post", _post)
    out = w.transcribe_words(clip)
    assert out["words"][0]["word"] == "hi"
    assert sent["data"]["timestamp_granularities[]"] == "word"
    assert "language" not in sent["data"], "an unknown language must be detected, not sent"
    assert sent["auth"] == "Bearer sk-test"


def test_api_errors_carry_the_status_and_never_return_empty(monkeypatch, clip):
    import httpx

    monkeypatch.setattr(w, "openai_key", lambda: "sk-test")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(401, text="bad key"))
    with pytest.raises(w.WordsError, match="401"):
        w.transcribe_words(clip, engine="whisper_api")


def test_api_without_a_key_says_how_to_get_one_or_go_local(monkeypatch, clip):
    monkeypatch.setattr(w, "openai_key", lambda: None)
    with pytest.raises(w.WordsError, match="OPENAI_API_KEY.*faster-whisper"):
        w.transcribe_words(clip, engine="whisper_api")


def test_a_file_over_the_api_cap_is_refused_before_uploading(monkeypatch, clip):
    monkeypatch.setattr(w, "openai_key", lambda: "sk-test")
    monkeypatch.setattr(w, "API_MAX_UPLOAD_MB", 0)
    with pytest.raises(w.WordsError, match="accepts 0MB"):
        w.transcribe_words(clip, engine="whisper_api")


def test_missing_audio_and_unknown_engine_are_errors(clip):
    with pytest.raises(w.WordsError, match="not found"):
        w.transcribe_words(clip.with_name("nope.mp3"))
    with pytest.raises(w.WordsError, match="unknown engine"):
        w.transcribe_words(clip, engine="carrier-pigeon")


def test_local_without_faster_whisper_says_what_to_install(monkeypatch, clip):
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    with pytest.raises(w.WordsError, match="pip install faster-whisper"):
        w.transcribe_words(clip, engine="whisper_local")
