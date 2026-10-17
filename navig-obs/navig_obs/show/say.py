"""A spoken line on stream: the broadcast pigeon reads a notice aloud.

Three steps, in this order:

1. **Synthesize** through navig's own TTS contract
   (``navig voice speak TEXT --provider P [--voice V] [--lang L] [--style S]
   --output FILE --no-play``) into ``state_dir()/tts/<sha16>.mp3``. ``--lang``
   makes names said the stage language's way (SCHEMA → Skeema / schéma /
   Схема); the card keeps the text as written. A clip already in the cache is
   reused, never re-synthesized — some providers bill per line.
   The voice's ``fx`` (the pigeon's pitch/tempo) is applied by ffmpeg after.
2. **Measure** the finished clip with ``ffprobe``; without one, estimate.
3. **Show** it in ONE OBS session: the ``schema-toast`` alert card, the pigeon's
   ``speak`` act for the same span, then the clip through the one cue input.

A failed synthesis never blocks the card: the line still shows, silently.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from navig_obs.show import actions, voices
from navig_obs.show.settings import ShowSettings, state_dir

TONES = ("deploy", "game", "live", "passport", "info")
MIN_MS = 3500
MS_PER_WORD = 380
CARD_TAIL_MS = 1000  # the card stays a beat after the pigeon stops talking
SYNTH_TIMEOUT_S = 120
PROBE_TIMEOUT_S = 15
FX_TIMEOUT_S = 60
DEFAULT_SAMPLE_RATE = 24000
_IMAGE_URL = re.compile(r"^https://[^\s\"'<>]{1,2000}$")


def clean_image(url: str | None) -> str | None:
    """An https image URL for the card, or None. Anything else never reaches
    the overlay (no file://, no javascript:, no http)."""
    u = (url or "").strip()
    return u if _IMAGE_URL.match(u) else None


@dataclass
class SayPlan:
    text: str
    title: str
    kicker: str
    tone: str
    provider: str
    voice: str | None
    voiced: bool
    lang: str | None = None
    style: str | None = None
    fx: str | None = None
    image: str | None = None
    # Who tries next when this voice fails (voices.chain minus the first).
    fallbacks: list[voices.Voice] = field(default_factory=list)
    # "provider/voice" that actually spoke (a fallback, when the first failed).
    spoke: str | None = None
    clip: Path | None = None
    cached: bool = False
    ms: int = 0
    warnings: list[str] = field(default_factory=list)

    def toast(self) -> dict[str, Any]:
        body = self.text if self.title != self.text else ""
        card = {
            "kind": "alert",
            "tone": self.tone,
            "kicker": self.kicker,
            "title": self.title,
            "text": body,
            "ms": self.ms + CARD_TAIL_MS,
        }
        if self.image:
            card["image"] = self.image
        return card

    def pigeon(self) -> dict[str, Any]:
        # The words ride along: the Grid Pigeon shows them in his speech
        # bubble while the clip plays (the same line the card already shows).
        return {"act": "speak", "ms": self.ms, "text": self.text}

    def to_json(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "provider": self.provider,
            "voice": self.voice,
            "lang": self.lang,
            "style": self.style,
            "fx": self.fx,
            "image": self.image,
            "spoke": self.spoke,
            "fallbacks": [f.label() for f in self.fallbacks],
            "voiced": self.voiced,
            "clip": str(self.clip) if self.clip else None,
            "cached": self.cached,
            "ms": self.ms,
            "toast": self.toast(),
            "pigeon": self.pigeon(),
            "warnings": list(self.warnings),
        }


def cache_dir() -> Path:
    return state_dir() / "tts"


def clip_key(
    provider: str,
    voice: str | None,
    text: str,
    lang: str | None = None,
    style: str | None = None,
    fx: str | None = None,
) -> str:
    parts = [provider, voice or "", text]
    if lang or style or fx:  # none of them keeps every older clip's key
        parts += [lang or "", style or "", fx or ""]
    raw = "\x00".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def clip_path(
    provider: str,
    voice: str | None,
    text: str,
    lang: str | None = None,
    style: str | None = None,
    fx: str | None = None,
) -> Path:
    return cache_dir() / f"{clip_key(provider, voice, text, lang, style, fx)}.mp3"


def estimate_ms(text: str) -> int:
    return max(MIN_MS, len(text.split()) * MS_PER_WORD)


def _navig_cmd() -> list[str]:
    exe = shutil.which("navig")
    return [exe] if exe else [sys.executable, "-m", "navig"]


def synthesize(
    text: str,
    provider: str,
    voice: str | None,
    out: Path,
    *,
    lang: str | None = None,
    style: str | None = None,
    fx: str | None = None,
) -> None:
    """Run the TTS contract into ``out`` (atomic: a half clip never lands in cache).

    Raises ``RuntimeError`` with the provider's last words on any failure.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".part.mp3")
    cmd = [*_navig_cmd(), "voice", "speak", text, "--provider", provider]
    if voice:
        cmd += ["--voice", voice]
    if lang:
        cmd += ["--lang", lang]
    if style:
        cmd += ["--style", style]
    cmd += ["--output", str(tmp), "--no-play"]
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=SYNTH_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"voice synthesis did not run ({exc})") from exc
    if proc.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or [""]
        tmp.unlink(missing_ok=True)
        raise RuntimeError(
            f"voice synthesis failed (exit {proc.returncode}) {tail[0]}".strip()
        )
    if fx:
        try:
            apply_fx(tmp, fx)
        except RuntimeError:
            tmp.unlink(missing_ok=True)
            raise
    tmp.replace(out)


def probe_rate(path: Path) -> int:
    """The clip's sample rate (ffprobe), or the TTS engines' usual 24 kHz."""
    exe = shutil.which("ffprobe")
    if exe:
        try:
            proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
                [exe, "-v", "error", "-select_streams", "a:0", "-show_entries",
                 "stream=sample_rate", "-of", "default=noprint_wrappers=1:nokey=1",
                 str(path)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=PROBE_TIMEOUT_S,
            )
            rate = int((proc.stdout or "").strip().splitlines()[0])
            if rate > 0:
                return rate
        except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
            pass
    return DEFAULT_SAMPLE_RATE


def apply_fx(path: Path, fx: str) -> None:
    """Give the clip the voice's character (pitch/tempo), in place.

    Raises ``RuntimeError`` on a bad spec or a missing/failed ffmpeg — a voice
    asked to sound like a pigeon never silently plays as a person.
    """
    try:
        chain = voices.fx_filter(fx, probe_rate(path))
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    if chain is None:
        return
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("the voice's fx needs ffmpeg on PATH")
    done = path.with_name(path.stem + ".fx.mp3")
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            [exe, "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
             "-af", chain, "-c:a", "libmp3lame", "-q:a", "3", str(done)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=FX_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"voice fx did not run ({exc})") from exc
    if proc.returncode != 0 or not done.is_file() or done.stat().st_size == 0:
        done.unlink(missing_ok=True)
        tail = (proc.stderr or "").strip().splitlines()[-1:] or [""]
        raise RuntimeError(f"voice fx failed (exit {proc.returncode}) {tail[0]}".strip())
    done.replace(path)


def probe_ms(path: Path) -> int | None:
    """Clip length in ms via ffprobe, or None when it cannot tell."""
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            [
                exe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PROBE_TIMEOUT_S,
        )
        seconds = float((proc.stdout or "").strip().splitlines()[0])
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        return None
    return int(round(seconds * 1000)) if seconds > 0 else None


def plan(
    text: str,
    *,
    title: str | None = None,
    kicker: str = "",
    tone: str = "info",
    provider: str = "edge",
    voice: str | None = None,
    voiced: bool = True,
    lang: str | None = None,
    style: str | None = None,
    fx: str | None = None,
    image: str | None = None,
    fresh: bool = False,
    fallbacks: list[voices.Voice] | None = None,
) -> SayPlan:
    """The plan without side effects: where the clip lives and whether it is cached.

    ``fresh`` ignores a cached clip (after a lexicon or voice change).
    """
    p = SayPlan(
        text=text,
        title=title or text,
        kicker=kicker,
        tone=tone,
        provider=provider,
        voice=voice,
        voiced=voiced,
        lang=lang,
        style=style,
        fx=fx,
        image=clean_image(image),
        fallbacks=list(fallbacks or []),
        ms=estimate_ms(text),
    )
    if voiced:
        p.clip = clip_path(provider, voice, text, lang, style, fx)
        p.cached = (not fresh) and p.clip.is_file() and p.clip.stat().st_size > 0
    return p


def voice_for(
    s: ShowSettings,
    lang: str | None,
    *,
    provider: str | None = None,
    voice: str | None = None,
) -> tuple[str, voices.Voice]:
    """(the language, its voice) for a line: ``--provider``/``--voice`` win, then
    the legacy one-voice-for-all settings, then the language's table entry."""
    code = voices.norm_lang(lang) or voices.norm_lang(s.say_lang) or voices.DEFAULT_LANG
    table = voices.table(s.say_voices)
    return code, voices.resolve(
        code,
        table,
        provider=provider or s.say_provider,
        voice=voice if voice is not None else (s.say_voice if not provider else None),
    )


def plan_for(
    s: ShowSettings,
    text: str,
    *,
    lang: str | None,
    provider: str | None = None,
    voice: str | None = None,
    **kw: Any,
) -> SayPlan:
    """A plan with the stage language's voice (style + fx included)."""
    code, v = voice_for(s, lang, provider=provider, voice=voice)
    return plan(
        text,
        provider=v.provider,
        voice=v.voice,
        lang=code,
        style=v.style,
        fx=v.fx,
        fallbacks=voices.chain(v, code)[1:],
        **kw,
    )


def prepare(p: SayPlan) -> SayPlan:
    """Synthesize (unless cached) and measure. Failure → silent card + a warning."""
    if not p.voiced or p.clip is None:
        return p
    first = f"{p.provider}/{p.voice or 'default'}"
    candidates = [(p.provider, p.voice, p.style, p.fx, p.clip, p.cached)]
    for f in p.fallbacks:
        c = clip_path(f.provider, f.voice, p.text, p.lang, f.style, f.fx)
        candidates.append((f.provider, f.voice, f.style, f.fx, c, None))
    for provider, voice, style, fx, clip, cached in candidates:
        if cached is None:
            cached = clip.is_file() and clip.stat().st_size > 0
        if not cached:
            try:
                synthesize(p.text, provider, voice, clip, lang=p.lang, style=style, fx=fx)
            except RuntimeError as exc:
                p.warnings.append(f"{provider}/{voice or 'default'}: {exc}")
                continue
        p.provider, p.voice, p.style, p.fx, p.clip = provider, voice, style, fx, clip
        p.cached = bool(cached)
        p.spoke = f"{provider}/{voice or 'default'}"
        if p.spoke != first:
            p.warnings.append(f"spoken by the fallback {p.spoke}")
        p.ms = probe_ms(clip) or estimate_ms(p.text)
        return p
    p.clip = None
    return p


def perform(
    client: actions.Requester,
    p: SayPlan,
    *,
    toast_event: str,
    pigeon_event: str,
    cue_input: str,
) -> None:
    """Card → pigeon → clip, on one connected client."""
    actions.emit_event(client, toast_event, p.toast())
    actions.emit_event(client, pigeon_event, p.pigeon())
    if p.clip is not None:
        actions.play_file(client, cue_input, p.clip)
