"""Turning a rough outline into a strict scenario — with a human in the middle.

Real episode notes look like the NobiCast EP0 rundown: numbered segments with durations,
bullet lists of talking points, a few finished sentences in quotes, links, reminders to
self. Roughly a third of it is meant to be spoken and the rest never is.

This step asks a model to sort that out and emit the strict format. It is deliberately a
**separate command** that writes a file you then edit, rather than a hidden stage inside
``render``. The reason is simple: if the model misjudges which lines are speech, the
mistake becomes audible only after you have paid to synthesise it. A file you read first
costs nothing to correct.

The output is validated by parsing it back before it is written, so ``draft`` cannot hand
you something ``render`` will reject.
"""

from __future__ import annotations

import re
from pathlib import Path

from navig.llm.generate import llm_generate
from navig_audio.podcast.scenario import Episode, ScenarioError, parse, slugify

_FENCE_RE = re.compile(r"^\s*```(?:markdown|md)?\s*\n|\n\s*```\s*$")

_SYSTEM = """You convert rough podcast notes into a strict scenario file.

OUTPUT FORMAT — nothing else, no commentary, no code fence:

---
episode: <number or omit>
slug: <kebab-case>
title: "<episode title>"
lang: <two-letter code of the SOURCE language>
default_speaker: <SPEAKER>
voices:
  <SPEAKER>: ""
---

## 01 — <segment title>

[music: <description of the music, in English>]
[sfx: <description of the effect, in English>]

<SPEAKER>: <the words to be spoken, verbatim prose>

RULES
1. One `##` heading per segment of the source, numbered 01, 02, 03 in order.
2. Only actual spoken words go in `SPEAKER:` lines. A bullet point that describes a
   topic ("explain the 5 pillars") is NOT speech.
3. Where the source already contains finished prose or a quoted line, keep it EXACTLY
   as written. Do not rewrite, polish or translate it.
4. Where a segment has only talking points and no prose, write natural spoken prose that
   covers them, in the source language, in the host's voice.
5. Audio directions (jingles, stings, ambience, transitions) become `[music: ...]` or
   `[sfx: ...]` cues placed where they occur. Never speak them.
6. Speaker names are ALL CAPS. List every speaker under `voices:` with an empty string.
7. Never invent facts, names, statistics or URLs that are not in the source.
"""


class DraftError(RuntimeError):
    """The outline could not be turned into a valid scenario."""


def _strip_fence(text: str) -> str:
    return _FENCE_RE.sub("", text.strip()).strip()


def draft_scenario(
    outline: str,
    *,
    lang: str | None = None,
    title: str | None = None,
    episode_number: int | None = None,
    default_speaker: str = "HOST",
) -> tuple[Episode, str]:
    """Convert rough notes into a strict scenario. Returns the parsed episode and its text."""
    if not outline.strip():
        raise DraftError("the outline is empty — there is nothing to convert")

    instructions = [f"Convert these notes into the strict scenario format.\n"]
    if lang:
        instructions.append(f"The source language is {lang!r}; keep it.")
    if title:
        instructions.append(f"Use this episode title: {title!r}.")
    if episode_number is not None:
        instructions.append(f"Set `episode: {episode_number}`.")
    instructions.append(f"Use {default_speaker!r} as the default speaker.")
    instructions.append(f"\n--- NOTES ---\n{outline}")

    raw = llm_generate(
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": "\n".join(instructions)},
        ],
        mode="chat",
        temperature=0.4,
    )
    text = _strip_fence(raw)
    if not text:
        raise DraftError("the model returned nothing")

    # Parsing before writing is the whole guarantee of this step: whatever `draft`
    # produces, `render` will accept.
    try:
        episode = parse(text)
    except ScenarioError as exc:
        raise DraftError(
            f"the model produced something that is not a valid scenario ({exc}). "
            f"Re-run, or write the frontmatter and `##` headings by hand."
        ) from exc

    return episode, text


def draft_file(
    source: Path,
    out: Path | None = None,
    *,
    lang: str | None = None,
    title: str | None = None,
    episode_number: int | None = None,
    default_speaker: str = "HOST",
) -> tuple[Episode, Path]:
    """Draft a scenario from a notes file and write it next to the source by default."""
    if not source.exists():
        raise DraftError(f"outline not found: {source}")
    outline = source.read_text(encoding="utf-8")

    episode, text = draft_scenario(
        outline,
        lang=lang,
        title=title,
        episode_number=episode_number,
        default_speaker=default_speaker,
    )

    if out is None:
        stem = slugify(episode.title, fallback=source.stem)
        prefix = f"ep{episode.number:03d}-" if episode.number is not None else ""
        out = source.parent / f"{prefix}{stem}.{episode.lang}.md"

    if out.exists():
        # Overwriting a scenario the user has since hand-edited would destroy the exact
        # work this two-step design exists to protect.
        raise DraftError(
            f"{out.name} already exists — pass a different --out, or delete it first "
            f"if you mean to replace your edits"
        )

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    return episode, out
