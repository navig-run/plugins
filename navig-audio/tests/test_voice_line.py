"""``navig audio voice-line`` — the cache contract is the whole product.

No test here may touch the network: a fresh synthesis is exercised with a
canned `with-timestamps` response (and a tiny ffmpeg-generated mp3 as its
audio), and the cache-hit path asserts the API transport is never called.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from navig_audio.voice import line as _line
from navig_audio.voice.line import (
    SETTINGS, SETTINGS_BY_LANG, LineError, deepen, line_key, resolve_voice,
    synth_timed,
)

runner_needed = shutil.which("ffmpeg")


def _original_key(text: str, voice: str, lang: str) -> str:
    """The channel scripts' key derivation, inlined — the contract under test."""
    vid = {"brian": "nPczCjzI2devNBz1zQrb"}.get(voice, voice)
    settings = SETTINGS_BY_LANG.get(lang, SETTINGS)
    return hashlib.sha1(
        json.dumps([text, vid, settings, "eleven_v4", lang], sort_keys=True).encode()
    ).hexdigest()[:16]


def test_key_matches_the_channel_scripts_derivation():
    for text in ("You know your neighbour's Wi-Fi.", "Найди своих.", "", "«266» — *red*"):
        for voice in ("brian", "nPczCjzI2devNBz1zQrb"):
            for lang in ("en", "ru", "fr"):
                assert line_key(text, voice, lang) == _original_key(text, voice, lang)


def test_cache_hit_never_touches_the_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    text = "Cached line."
    key = line_key(text, "brian", "en")
    cache = tmp_path / "vo"
    cache.mkdir()
    (cache / f"{key}.mp3").write_bytes(b"ID3-cached-bytes")
    (cache / f"{key}.json").write_text(
        json.dumps([{"w": "Cached", "s": 0.0, "e": 0.4}]), encoding="utf-8")

    def boom(*a, **k):  # pragma: no cover — the guard
        raise AssertionError("the API was called on a cache hit")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    r = synth_timed(text, "brian", "en", cache)
    assert r.cached is True
    assert r.path.read_bytes() == b"ID3-cached-bytes"
    assert r.words[0]["w"] == "Cached"


def test_cache_hit_still_writes_no_semi_when_the_pitch_is_right(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A legacy entry without .semi must NOT be re-pitched when it already matches."""
    text = "Legacy entry."
    key = line_key(text, "brian", "en")
    cache = tmp_path
    payload = b"LEGACY-MP3"
    (cache / f"{key}.mp3").write_bytes(payload)
    (cache / f"{key}.json").write_text("[]", encoding="utf-8")
    # no .semi file at all; per-language want is -3.0 for en
    called = []
    monkeypatch.setattr("navig_audio.voice.line.subprocess.run", lambda *a, **k: called.append(a))
    r = synth_timed(text, "brian", "en", cache)
    assert r.cached and not called, "re-pitched an entry that already matches"
    assert (cache / f"{key}.mp3").read_bytes() == payload


@pytest.mark.skipif(not runner_needed, reason="ffmpeg not on PATH")
def test_fresh_synthesis_writes_mp3_words_and_semi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Canned API response → deepened mp3 + timings + .semi. Zero real network."""
    # a valid tiny mp3 for deepen() to chew on
    raw_src = tmp_path / "tone.wav"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=220:duration=0.3",
         str(raw_src)], check=True)
    payload = {
        "audio_base64": __import__("base64").b64encode(
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-i", str(raw_src), "-f", "mp3", "-"],
                capture_output=True).stdout).decode(),
        "alignment": {
            "characters": list("Hi."),
            "character_start_times_seconds": [0.0, 0.05, 0.1],
            "character_end_times_seconds": [0.05, 0.1, 0.2],
        },
    }

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps(payload).encode()

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: FakeResp())
    cache = tmp_path / "vo"
    r = synth_timed("Hi.", "brian", "en", cache)
    assert r.cached is False
    assert r.path.exists() and r.timings_path.exists()
    assert (cache / f"{r.key}.semi").read_text() == "-3.0"
    assert [w["w"] for w in r.words] == ["Hi."]
    # second call: served from disk, transport untouched
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("API on hit")))
    r2 = synth_timed("Hi.", "brian", "en", cache)
    assert r2.cached and r2.path == r.path


def test_deepen_keeps_duration(tmp_path: Path):
    """asetrate+atempo must not change the length — the timestamps depend on it."""
    if not runner_needed:
        pytest.skip("ffmpeg not on PATH")
    src = tmp_path / "in.wav"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=110:duration=1.0",
         str(src)], check=True)
    out = tmp_path / "out.mp3"
    deepen(src, out, -3.0)
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
        capture_output=True, text=True)
    assert abs(float(r.stdout.strip()) - 1.0) < 0.1


def test_missing_key_is_a_line_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.delenv("ELEVEN_API_KEY", raising=False)

    class NoVault:
        def get_api_key(self, *a, **k): return None

    import navig_sdk.keys as keys

    monkeypatch.setattr(keys, "api_key", lambda *a, **k: "")
    with pytest.raises(LineError, match="ElevenLabs key"):
        synth_timed("x", "brian", "en", tmp_path, force=True)


# ---------------------------------------------------------------- roster ---


def test_channel_cast_names_resolve_to_their_ids():
    """wave1 CAST.md names must reach ElevenLabs, not fall through as ids.

    Before: ``resolve_voice`` returned the name untouched, so
    ``navig audio voice-line --voice stanislav …`` asked the API for a voice
    literally called "stanislav" and 404'd — reported as a key problem.
    """
    for name, vid in (
        ("stanislav", "ogi2DyUAKJb7CEdqqvlU"),   # RU narrator
        ("maxim", "gMIlPNegT3C1SdNBp6rW"),       # RU spare
        ("guillaume", "3HZyQcLKlT0a3RDeXVsP"),   # FR narrator
        ("marcus", "iGmGS5XcCI1uvcBqkAlM"),      # Digital Ghost
        ("callum", "N2lVS1w4EtoT3dr4eOWO"),      # void / the Architect
        ("bella", "hpp4J3VqNfWAUOO0d1Us"),       # Ensina Nova
    ):
        assert resolve_voice(name) == vid


def test_raw_ids_still_pass_through():
    """A raw id must be untouched — it is how the id roster is escaped."""
    for vid in ("ogi2DyUAKJb7CEdqqvlU", "nPczCjzI2devNBz1zQrb", "a" * 20):
        assert resolve_voice(vid) == vid


def test_unknown_name_fails_instead_of_404ing():
    """An unknown NAME must raise here, not become a literal API path.

    This is the regression: the old code did ``VOICES.get(v, (v, ""))[0]``,
    which turns a typo into a voice id and costs a network round-trip whose
    error blames the key rather than the voice.
    """
    with pytest.raises(LineError, match="stanisla"):
        resolve_voice("stanisla")          # the actual typo
    with pytest.raises(LineError, match="Roster:"):
        resolve_voice("not-a-voice")


#: The roster as it stood before this fix — the resolver must be identical
#: on every input that ever produced a cache entry.
_PRE_FIX_VOICES = {
    "brian": "nPczCjzI2devNBz1zQrb",
    "charlie": "IKne3meq5aSn9XLyUdCD",
    "adam": "pNInz6obpgDQGcFmaJgB",
    "george": "JBFqnCBsd6RMkjVDRZzb",
    "bill": "pqHfZKP75CvOlQylNhV4",
    "daniel": "onwK4e9ZLuTAKqWW03F9",
}


def test_cache_keys_are_unchanged_for_everything_that_already_worked():
    """The roster fix must not invalidate a single cached line.

    Keys are derived from the resolved id, so a changed id is a cache miss
    and a re-bill. Every input that resolved before must resolve to the same
    id now; inputs that 404'd never wrote a cache entry in the first place.
    """
    inputs = list(_PRE_FIX_VOICES) + ["nPczCjzI2devNBz1zQrb", "ogi2DyUAKJb7CEdqqvlU"]
    for voice in inputs:
        old_vid = _PRE_FIX_VOICES.get(voice, voice)   # pre-fix behaviour
        assert resolve_voice(voice) == old_vid, voice
        assert (line_key("проверка", voice, "ru")
                == line_key("проверка", old_vid, "ru")), voice


# ------------------------------------------------------------ semitones ---


def test_semitones_overrides_the_language_default(tmp_path: Path, monkeypatch) -> None:
    text, voice, lang = "Hi there", "daniel", "en"
    key = _line.line_key(text, voice, lang)
    (tmp_path / f"{key}.mp3").write_bytes(b"fake-audio")
    (tmp_path / f"{key}.json").write_text(
        '[{"w": "Hi", "s": 0.0, "e": 0.4}, {"w": "there", "s": 0.4, "e": 0.9}]', encoding="utf-8")
    (tmp_path / f"{key}.semi").write_text(str(_line.SEMITONES[lang]), encoding="utf-8")  # as rendered before
    pitched: list[float] = []
    monkeypatch.setattr(_line, "deepen",
                        lambda src, out, semis: (pitched.append(semis),
                                                 Path(out).write_bytes(b"re-pitched")))
    r = _line.synth_timed(text, voice, lang, tmp_path, semitones=-1.5)
    # old code: want stayed at SEMITONES[en] (-3.0) → no re-pitch, file still "-3.0"
    assert float(r.path.with_suffix(".semi").read_text(encoding="utf-8")) == -1.5
    # deepen applies the DELTA from the stored pitch: -3.0 -> -1.5 is a +1.5 shift
    assert pitched and round(pitched[0], 3) == round(-1.5 - _line.SEMITONES[lang], 3)
    # and the language default still applies when the caller passes nothing
    (tmp_path / f"{key}.mp3").write_bytes(b"fake-audio")
    r2 = _line.synth_timed(text, voice, lang, tmp_path)
    assert float(r2.path.with_suffix(".semi").read_text(encoding="utf-8")) == float(_line.SEMITONES[lang])
