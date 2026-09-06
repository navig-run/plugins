"""Translating an episode — the words change, the structure does not.

The obvious implementation, handing the whole Markdown file to a model and asking for it
back in another language, is the wrong one. Frontmatter gets reformatted, speaker tags
get localised, cue lines get translated into prose, heading numbers drift — and the
result no longer parses, or parses into a different album than the original.

So only the spoken lines are ever sent. Everything structural — track numbers, titles'
positions, speaker names, ``[music:]`` and ``[sfx:]`` cues, the voice map — is carried
across mechanically. The translated episode is guaranteed to have the same tracks in the
same order with the same speakers, which is what makes a bilingual release coherent.

Lines are translated a track at a time so the model can see how a passage flows, and the
response is checked to have exactly as many lines as were sent. A mismatch is repaired
line by line rather than accepted, because a silently dropped line is an episode with a
hole in it.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any

from navig.llm.generate import llm_generate
from navig_audio.podcast.scenario import Cue, Episode, Line, Track, slugify

# Names shown to the model, so the instruction reads as a language rather than a code.
LANGUAGE_NAMES = {
    "en": "English",
    "fr": "French",
    "ru": "Russian",
    "es": "Spanish",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
    "nl": "Dutch",
    "pl": "Polish",
    "ja": "Japanese",
    "ko": "Korean",
    "zh": "Chinese",
    "ar": "Arabic",
}

_SYSTEM = (
    "You translate podcast scripts. You are translating speech that will be read aloud, "
    "not written prose: keep the speaker's rhythm, register and humour, keep contractions "
    "natural, and keep sentences speakable. Preserve proper nouns, brand names and "
    "technical terms as they are. Never add, merge, drop, explain or summarise anything."
)

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


class TranslationError(RuntimeError):
    """The model's output could not be used. Always says what went wrong."""


def language_name(code: str) -> str:
    return LANGUAGE_NAMES.get(code.lower(), code)


def _strip_fence(text: str) -> str:
    """Models wrap JSON in code fences about half the time; both forms are fine."""
    return _FENCE_RE.sub("", text.strip()).strip()


def _ask(prompt: str, *, temperature: float = 0.3) -> str:
    return llm_generate(
        messages=[{"role": "system", "content": _SYSTEM}, {"role": "user", "content": prompt}],
        mode="chat",
        temperature=temperature,
    )


def _translate_batch(texts: list[str], target: str, *, context: str) -> list[str]:
    """Translate a list of lines, insisting on a same-length JSON array back."""
    if not texts:
        return []
    payload = json.dumps(texts, ensure_ascii=False, indent=1)
    prompt = (
        f"Translate each string in this JSON array into {language_name(target)}.\n"
        f"Context: this is the podcast segment {context!r}.\n\n"
        f"Return ONLY a JSON array of exactly {len(texts)} strings, in the same order. "
        f"No commentary, no code fence, no keys.\n\n{payload}"
    )
    raw = _ask(prompt)
    try:
        result = json.loads(_strip_fence(raw))
    except json.JSONDecodeError:
        result = None

    if isinstance(result, list) and len(result) == len(texts):
        return [str(item) for item in result]

    # One retry, then per-line. Falling back rather than failing matters here: a long
    # episode is a lot of work to lose to one malformed response.
    raw = _ask(
        f"Your previous answer was not a JSON array of exactly {len(texts)} strings. "
        f"Return ONLY that array, nothing else.\n\n{payload}",
        temperature=0.0,
    )
    try:
        result = json.loads(_strip_fence(raw))
    except json.JSONDecodeError:
        result = None
    if isinstance(result, list) and len(result) == len(texts):
        return [str(item) for item in result]

    return [_translate_one(text, target) for text in texts]


def _translate_one(text: str, target: str) -> str:
    """Last resort: one line at a time, where nothing can be misaligned."""
    out = _ask(
        f"Translate the following line into {language_name(target)}. "
        f"Return only the translation, with no quotes and no commentary.\n\n{text}",
        temperature=0.2,
    ).strip()
    if not out:
        raise TranslationError(f"the model returned nothing for: {text[:60]!r}")
    return _strip_fence(out).strip('"')


def _translate_track(track: Track, target: str) -> Track:
    lines = [seg for seg in track.segments if isinstance(seg, Line)]
    translated = _translate_batch([line.text for line in lines], target, context=track.title)

    out_segments: list[Line | Cue] = []
    cursor = 0
    for segment in track.segments:
        if isinstance(segment, Cue):
            # Cues are generation prompts, not dialogue — translating "cyberpunk modem
            # swell" would change the sound, not localise it.
            out_segments.append(segment)
            continue
        out_segments.append(replace(segment, text=translated[cursor]))
        cursor += 1

    title = _translate_one(track.title, target) if track.title.strip() else track.title
    return Track(
        number=track.number,
        title=title,
        slug=slugify(title, fallback=track.slug),
        segments=out_segments,
    )


def translate_episode(episode: Episode, target: str, *, progress: Any = None) -> Episode:
    """Return a copy of ``episode`` with every spoken line rendered into ``target``.

    The voice map is carried over unchanged. That is usually right — a cloned voice
    speaks every language it supports — but see the note in the README about accent
    bleed if the clone was trained on one language only.
    """
    target = target.lower()
    if target == episode.lang:
        raise TranslationError(
            f"the scenario is already in {language_name(target)} — "
            f"pass a different language to `--to`"
        )

    tracks = []
    for track in episode.tracks:
        if progress:
            progress(f"  translating {track.number:02d} — {track.title}")
        tracks.append(_translate_track(track, target))

    title = _translate_one(episode.title, target) if episode.title.strip() else episode.title
    return Episode(
        title=title,
        # The slug stays put: it identifies the episode across languages, and a
        # translated slug would make the same episode look like two different ones.
        slug=episode.slug,
        lang=target,
        tracks=tracks,
        number=episode.number,
        model=episode.model,
        default_speaker=episode.default_speaker,
        voices=dict(episode.voices),
        source=None,
        meta=dict(episode.meta),
    )
