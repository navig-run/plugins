"""One timed narration line — ElevenLabs with the settings the channel was tuned on.

Ported from the channel workspace's ``say.py`` and ``kinetic.py`` (the two were
one engine split across files). Three things the plain TTS path does not do,
and together they are why the channel's reads sound the way they do:

* **192 kbps + real ``voice_settings``** — the default path sends no settings at
  all and asks for 128, which on a low resonant voice throws away exactly the
  bottom that makes it sound expensive;
* **word timestamps** (``/with-timestamps``) — the kinetic renderer cuts where
  the voice cuts, so timing is read, never guessed;
* **a deepen pass** — a small pitch drop (per language) plus a low shelf, with
  ``asetrate``+``atempo`` so the duration — and therefore the timestamps —
  stay true.

**The cache is the contract.** Lines are keyed by
``sha1([text, voice-id, settings, model, lang])`` — byte-identical to the
workspace scripts' key — and stored where the channel keeps its voice
(``<workspace>/vo/kinetic``). The renderer and these verbs share it, so a
re-render never re-bills, and a deepen change re-pitches from the stored raw
instead of synthesizing again.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import urllib.request
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

__all__ = [
    "BASE",
    "FORMAT",
    "MODEL",
    "SEMITONES",
    "SETTINGS",
    "SETTINGS_BY_LANG",
    "SHELF_GAIN",
    "LineError",
    "LineResult",
    "VOICES",
    "duration",
    "synth_timed",
]

BASE = "https://api.elevenlabs.io/v1"
MODEL = "eleven_v4"          # tried 2026-10-12: works with /with-timestamps (43/43 aligned)
FORMAT = "mp3_44100_192"     # the default path asks for 128; the channel needs 192

#: The deep end of the roster, named exactly as ElevenLabs describes them.
VOICES = {
    "brian": ("nPczCjzI2devNBz1zQrb", "Deep, Resonant and Comforting"),
    "charlie": ("IKne3meq5aSn9XLyUdCD", "Deep, Confident, Energetic"),
    "adam": ("pNInz6obpgDQGcFmaJgB", "Dominant, Firm"),
    "george": ("JBFqnCBsd6RMkjVDRZzb", "Warm, Captivating Storyteller"),
    "bill": ("pqHfZKP75CvOlQylNhV4", "Wise, Mature, Balanced"),
    "daniel": ("onwK4e9ZLuTAKqWW03F9", "Steady Broadcaster"),
    # The channel's own cast (wave1 CAST.md) — non-English narrators and the
    # character bench. These were documented but never registered, so
    # `--voice stanislav` fell through to the id slot and 404'd at the API.
    "bella": ("hpp4J3VqNfWAUOO0d1Us", "Playful, Bright, Warm"),
    "marcus": ("iGmGS5XcCI1uvcBqkAlM", "Authoritative and Deep"),
    "callum": ("N2lVS1w4EtoT3dr4eOWO", "Husky Trickster"),
    "roger": ("CwhRBWXzGAHq8TQ4Fs17", "Laid-Back, Casual, Resonant"),
    "stanislav": ("ogi2DyUAKJb7CEdqqvlU", "RU narrator (channel)"),
    "maxim": ("gMIlPNegT3C1SdNBp6rW", "RU spare"),
    "guillaume": ("3HZyQcLKlT0a3RDeXVsP", "FR narrator (channel)"),
}

#: ElevenLabs voice ids are 20 alphanumerics; a roster name never is. Sized
#: 16–40 so an id-format change can't be rejected as a typo — the only thing
#: this must catch is a short lowercase name like "stanislav".
_ID_SHAPE = re.compile(r"^[0-9A-Za-z_-]{16,40}$")

#: Low stability lets the read breathe; high similarity keeps the timbre; a
#: little style buys drama without tipping into performance.
SETTINGS = {
    "stability": 0.38,
    "similarity_boost": 0.88,
    "style": 0.35,
    "use_speaker_boost": True,
}

# Per-language tuning from the channel (owner notes in the original sources):
# EN steadier pacing, every consonant placed (0.60/0.12); FR steadier so the
# consonants come back (0.55/0.18). RU keeps the shared settings.
SETTINGS_BY_LANG = {
    "en": {"stability": 0.60, "similarity_boost": 0.88, "style": 0.12,
           "use_speaker_boost": True},
    "fr": {"stability": 0.55, "similarity_boost": 0.9, "style": 0.18,
           "use_speaker_boost": True},
}

# Weight added after synthesis, not asked of the model. Native RU/FR voices are
# already low and a pitch drop on them reads as "slowed down" — they get only
# the shelf. -3.0: the owner's note on the EN render — "it's not deep."
SEMITONES = {"en": -3.0, "ru": -0.4, "fr": -0.5}
SHELF_GAIN = 3.5             # dB at 110 Hz


class LineError(RuntimeError):
    """Synthesis failed; the message says what to do about it."""


@dataclass
class LineResult:
    path: Path                 # the deepened mp3
    words: list[dict]          # [{"w", "s", "e"}] — seconds, voice-time
    key: str                   # cache key (16 hex chars)
    cached: bool               # True = served from the voice cache, no API call

    @property
    def timings_path(self) -> Path:
        return self.path.with_suffix(".json")


def _api_key() -> str:
    """env first, then the shared vault — never a key on the command line."""
    from navig_sdk.keys import api_key

    key = api_key("elevenlabs", "ELEVENLABS_API_KEY", "ELEVEN_API_KEY", caller="navig_audio")
    if not key:
        raise LineError(
            "no ElevenLabs key: set ELEVENLABS_API_KEY or store one with "
            "`navig vault add elevenlabs`")
    return key


def resolve_voice(voice: str) -> str:
    """A roster name → its id; a raw id passes through.

    An unrecognised *name* used to fall through to the id slot untouched, so
    the API answered 404 with a message about a key rather than about the
    voice. Anything that isn't a 20-alphanumeric id is a name, and a name I
    don't know is a typo — say so here, where the roster is one line away.
    """
    hit = VOICES.get(voice)
    if hit:
        return hit[0]
    if _ID_SHAPE.match(voice):
        return voice
    known = ", ".join(sorted(VOICES))
    raise LineError(
        f"unknown voice {voice!r} — not a roster name and not a voice id. "
        f"Roster: {known}. A raw ElevenLabs id still works as-is."
    )


def line_key(text: str, voice: str, lang: str) -> str:
    """The cache key — byte-identical to the channel scripts' derivation."""
    vid = resolve_voice(voice)
    settings = SETTINGS_BY_LANG.get(lang, SETTINGS)
    return hashlib.sha1(
        json.dumps([text, vid, settings, MODEL, lang], sort_keys=True).encode()
    ).hexdigest()[:16]


def deepen(src: Path, out: Path, semis: float) -> None:
    """Drop the pitch a little and lift the chest, without changing the pace.

    ``asetrate`` alone would slow the read as it lowers it, so the tempo is put
    back with ``atempo`` — which is what keeps the word timestamps true.
    """
    ratio = 2 ** (semis / 12)
    af = (f"asetrate=44100*{ratio:.6f},aresample=44100,atempo={1 / ratio:.6f},"
          f"equalizer=f=110:t=q:w=0.9:g={SHELF_GAIN},"
          f"equalizer=f=3200:t=q:w=1.2:g=1.5,"
          f"acompressor=threshold=-18dB:ratio=2.5:attack=8:release=160,"
          f"loudnorm=I=-16:TP=-1.5:LRA=9")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-af", af,
                    "-ar", "44100", "-b:a", "192k", str(out)], check=True)
    # the raw is kept: the next deepen change re-derives from it exactly


def _words(chars, starts, ends) -> list[dict]:
    out, cur, s0, e0 = [], "", None, 0.0
    for c, s, e in zip(chars, starts, ends):
        if c.isspace():
            if cur:
                out.append({"w": cur, "s": s0, "e": e0})
            cur, s0 = "", None
            continue
        if s0 is None:
            s0 = s
        cur += c
        e0 = e
    if cur:
        out.append({"w": cur, "s": s0, "e": e0})
    return out


def synth_timed(
    text: str,
    voice: str,
    lang: str,
    cache_dir: Path,
    force: bool = False,
    semitones: Optional[float] = None,
) -> LineResult:
    """One line with word timings, cached by content.

    ``force=True`` ignores an existing cache entry (re-bills). ``semitones``
    overrides the per-language deepen — a change that re-pitches from the raw
    and never touches the API.
    """
    vid = resolve_voice(voice)
    key = line_key(text, voice, lang)
    mp3 = Path(cache_dir) / f"{key}.mp3"
    meta = mp3.with_suffix(".json")
    raw = mp3.with_suffix(".raw.mp3")
    semi = mp3.with_suffix(".semi")
    # the override must win — as written, both branches produced the language default and
    # `semitones=` silently did nothing (the docstring promised an override)
    want = semitones if semitones is not None else SEMITONES.get(lang, 0.0)

    if mp3.exists() and meta.exists() and not force:
        # a deepen change re-pitches from the raw (or composes a delta on
        # legacy files) — never re-bills the API just to sound deeper
        done = float(semi.read_text(encoding="utf-8")) if semi.exists() else None
        if abs((done if done is not None else want) - want) > 0.05:
            if raw.exists():
                deepen(raw, mp3, want)
            else:
                legacy = done if done is not None else (-1.2 if lang == "en" else want)
                if abs(legacy - want) > 0.05:
                    tmp = mp3.with_suffix(".tmp.mp3")
                    deepen(mp3, tmp, want - legacy)
                    tmp.replace(mp3)
            semi.write_text(str(want), encoding="utf-8")
        return LineResult(mp3, json.loads(meta.read_text(encoding="utf-8")), key, True)

    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    settings = SETTINGS_BY_LANG.get(lang, SETTINGS)
    body = {"text": text, "model_id": MODEL, "voice_settings": settings}
    req = urllib.request.Request(
        f"{BASE}/text-to-speech/{vid}/with-timestamps?output_format={FORMAT}",
        data=json.dumps(body).encode(),
        headers={"xi-api-key": _api_key(), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:  # noqa: S310 — https, fixed host
            d = json.load(r)
    except OSError as exc:
        raise LineError(
            f"ElevenLabs synthesis failed: {exc}. Check the key "
            "(`navig vault get elevenlabs --reveal`) and your quota."
        ) from exc
    raw.write_bytes(base64.b64decode(d["audio_base64"]))
    # the ORIGINAL alignment: the normalized one rewrites "—" as "--"
    al = d.get("alignment") or d.get("normalized_alignment")
    if not al:
        raise LineError("ElevenLabs returned no alignment — cannot time the words")
    words = _words(al["characters"], al["character_start_times_seconds"],
                   al["character_end_times_seconds"])
    deepen(raw, mp3, want)
    meta.write_text(json.dumps(words, ensure_ascii=False), encoding="utf-8")
    semi.write_text(str(want), encoding="utf-8")
    return LineResult(mp3, words, key, False)


def duration(p: Path) -> float:
    """Seconds, via ffprobe — the timeline's unit of truth (bounded: a stuck probe errors)."""
    from navig_media_core.media.audio_edit import AudioEditError, probe_duration  # noqa: PLC0415

    try:
        return probe_duration(p)
    except AudioEditError as exc:
        raise LineError(f"ffprobe gave no duration for {p}") from exc


def cache_stats(cache_dir: Path) -> dict:
    """Counts, sizes, orphans and age of the shared voice cache — read-only.

    A usable line is a ``<key>.mp3`` + ``<key>.json`` pair: the renderer needs
    both (audio and word timings), and each one present is a future hit that
    never re-bills. An orphan is the opposite — timings without audio, audio
    without timings, or a stored raw whose final was deleted — so the next
    render of that line synthesizes (and bills) again. ``.tmp.mp3`` leftovers
    are crash debris: harmless, but they say a write died mid-file.
    """
    cache_dir = Path(cache_dir)
    exists = cache_dir.is_dir()
    files = sorted(cache_dir.iterdir()) if exists else []
    finals: list[Path] = []
    metas: list[Path] = []
    raws: list[Path] = []
    semis: list[Path] = []
    tmps: list[Path] = []
    for f in files:
        name = f.name
        if name.endswith(".raw.mp3"):
            raws.append(f)
        elif name.endswith(".tmp.mp3"):
            tmps.append(f)
        elif f.suffix == ".mp3":
            finals.append(f)
        elif f.suffix == ".json":
            metas.append(f)
        elif f.suffix == ".semi":
            semis.append(f)
    final_keys = {f.stem for f in finals}
    meta_keys = {f.stem for f in metas}
    raw_keys = {f.name[: -len(".raw.mp3")] for f in raws}
    stamps = sorted(f.stat().st_mtime for f in finals)
    return {
        "dir": str(cache_dir),
        "exists": exists,
        "mp3": len(finals),
        "json": len(metas),
        "raw": len(raws),
        "semi": len(semis),
        "tmp": len(tmps),
        "bytes": sum(f.stat().st_size for f in files),
        "orphans": {
            "json_without_mp3": sorted(meta_keys - final_keys),
            "mp3_without_json": sorted(final_keys - meta_keys),
            "raw_without_mp3": sorted(raw_keys - final_keys),
        },
        "oldest": datetime.fromtimestamp(stamps[0]).isoformat(timespec="seconds") if stamps else None,
        "newest": datetime.fromtimestamp(stamps[-1]).isoformat(timespec="seconds") if stamps else None,
    }
