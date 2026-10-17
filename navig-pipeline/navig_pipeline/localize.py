"""One capture, many locales — a scene's words in another language, same picture.

Everything a viewer READS is collected (hook title and sub, captions, karaoke cues,
outro tag and follow line), translated as one batch, and written back as
``scene.<loc>.json`` beside the source. What a viewer reads INSIDE a terminal is a real
capture and is never translated — a localized video that rewrote command output would be
a fabricated one.

**Timing (v1).** A translated cue keeps its English window, and its words are spread
evenly across it — the stage already does exactly that when a cue's words and timings
disagree. The phrase lands with the voice; individual words are approximate. True
word-level karaoke in another language needs that language's narration (re-synthesized
lines give real timings), which is what ``rebuild_episode.py`` does once a translated
``rebuild.json`` exists.

Translations come from ``strings`` first (an operator-supplied ``{english: localized}``
file — reviewed copy beats generated copy) and only then from the model, through the
SDK's ``generate`` seam like every other plugin.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable
from typing import Any

Translate = Callable[[list[str], str], list[str]]

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


class LocalizeError(RuntimeError):
    """The scene could not be localized."""


def _texts(scene: dict[str, Any]) -> list[str]:
    """Every viewer-facing string, in a stable order, de-duplicated."""
    seen: dict[str, None] = {}
    for seg in scene.get("segments") or []:
        for key in ("title", "sub", "caption", "tag", "follow", "label"):
            if seg.get("type") == "terminal" and key == "title":
                continue  # a terminal title is the window bar (`~ prod-01`), not prose
            if isinstance(seg.get(key), str) and seg[key].strip():
                seen.setdefault(seg[key], None)
        for cue in ((seg.get("speech") or {}).get("cues")) or []:
            if cue and str(cue[0]).strip():
                seen.setdefault(str(cue[0]), None)
    return list(seen)


def _spread(text: str, t0: float, t1: float) -> list[list[Any]]:
    words = text.split()
    if not words:
        return []
    step = max(0.0, t1 - t0) / len(words)
    return [[w, round(t0 + i * step, 3), round(t0 + (i + 1) * step, 3)] for i, w in enumerate(words)]


def localize(scene: dict[str, Any], lang: str, translate: Translate,
             strings: dict[str, str] | None = None) -> tuple[dict[str, Any], dict[str, int]]:
    """Return the localized scene and counts: ``{"strings": supplied, "model": generated}``."""
    strings = dict(strings or {})
    texts = _texts(scene)
    todo = [t for t in texts if t not in strings]
    if todo:
        out = translate(todo, lang)
        if len(out) != len(todo):
            raise LocalizeError(f"translator returned {len(out)} strings for {len(todo)}")
        strings.update({src: dst for src, dst in zip(todo, out)})
    result = copy.deepcopy(scene)
    for seg in result.get("segments") or []:
        for key in ("title", "sub", "caption", "tag", "follow", "label"):
            if seg.get("type") == "terminal" and key == "title":
                continue
            if isinstance(seg.get(key), str) and seg[key] in strings:
                seg[key] = strings[seg[key]]
        sp = seg.get("speech")
        if sp and sp.get("cues"):
            cues = [[strings.get(str(c[0]), str(c[0])), c[1], c[2]] for c in sp["cues"]]
            sp["cues"] = cues
            sp["words"] = [w for c in cues for w in _spread(c[0], float(c[1]), float(c[2]))]
    result["lang"] = lang
    return result, {"strings": len(texts) - len(todo), "model": len(todo)}


def model_translator(*, context: str) -> Translate:
    """The SDK seam: one JSON-array round trip, one retry, then line by line."""
    from navig_sdk.ai import AIError, generate

    system = ("You translate short on-screen video copy. Keep it punchy and natural for a "
              "phone screen; keep product and command names (navig, nginx, Quest) unchanged; "
              "keep *asterisk emphasis* markers around the equivalent words.")

    def ask(prompt: str, temperature: float) -> str:
        try:
            return generate(prompt, system=system, temperature=temperature, mode="chat")
        except AIError as exc:
            raise LocalizeError(str(exc)) from exc

    def run(texts: list[str], lang: str) -> list[str]:
        payload = json.dumps(texts, ensure_ascii=False, indent=1)
        for temperature in (0.3, 0.0):
            raw = ask(f"Translate each string into the language with code {lang!r}. Context: {context}.\n"
                      f"Return ONLY a JSON array of exactly {len(texts)} strings, same order.\n\n{payload}",
                      temperature)
            try:
                got = json.loads(_FENCE.sub("", raw.strip()))
            except ValueError:
                continue
            if isinstance(got, list) and len(got) == len(texts):
                return [str(x) for x in got]
        return [ask(f"Translate into {lang!r}; reply with the translation only:\n\n{t}", 0.0).strip().strip('"')
                for t in texts]

    return run


def localize_plan(cfg: dict[str, Any], lang: str, translate: Translate,
                  strings: dict[str, str] | None = None) -> tuple[dict[str, Any], dict[str, int]]:
    """A narration plan in another language — same cuts, same voices, new words.

    Cuts are expressed as "after line X, hold H" (see ``episode.plan_from_legacy``), so they
    land correctly however much longer or shorter the translated lines turn out. The plan
    writes to its own voice folder, scene and shotlist, so the original cut is untouched.
    """
    strings = dict(strings or {})
    texts = [ln[2] for ln in cfg["lines"]]
    todo = [t for t in dict.fromkeys(texts) if t not in strings]
    if todo:
        out = translate(todo, lang)
        if len(out) != len(todo):
            raise LocalizeError(f"translator returned {len(out)} lines for {len(todo)}")
        strings.update(dict(zip(todo, out)))
    plan = copy.deepcopy(cfg)
    plan["lines"] = [[lid, voice, strings.get(text, text), gap] for lid, voice, text, gap in cfg["lines"]]
    plan["lang"] = lang
    scene = cfg.get("scene", "scene.json")
    clip = cfg.get("clip", "clip-v2.md")
    plan["scene"] = scene.removesuffix(".json") + f".{lang}.json"
    plan["clip"] = clip.removesuffix(".md") + f"-{lang}.md"
    plan["vo_dir"] = f"{cfg.get('vo_dir', '_vo')}-{lang}"
    plan["title"] = f"{cfg.get('title', 'episode')} ({lang})"
    plan["updates"] = []  # pacing updates already live in the source scene the copy came from
    return plan, {"strings": len(texts) - len(todo), "model": len(todo)}
