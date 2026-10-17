"""Draft an episode from an article — a starting cut, with every claim on a leash.

A campaign article already carries verified commands and classified claims. ``draft``
asks the model (through the SDK seam, like every plugin) for a short-form episode built
ONLY from that material — hook, 2-4 command beats, narration, posting copy — then checks
the answer instead of trusting it:

* every beat's command must resolve to a real ``navig`` command in the shipped manifest
  (``core/generated/commands.json``); one that does not is reported and NOT written;
* every number in the hook, narration or caption must appear in the article — an
  unsourced number is reported (after capture, ``qa`` holds the same numbers to the
  FOOTAGE, which is the check that would have caught E05's "840").

It writes a folder `episode build` understands: ``scene.json`` (casts named, not yet
recorded), ``rebuild.json`` (narration plan), ``posting.json`` and ``CAPTURE.md`` — the
exact ``navig pipeline cast`` lines to record. Nothing is captured, voiced or rendered:
a draft is the blank page filled in, not footage invented.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

Ask = Callable[[str], str]

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")
_NUMBER = re.compile(r"(?<![\w.])\d[\d,.]*\d|(?<![\w.])\d(?![\w])")
#: The shipped command manifest, relative to this file in a checkout (plugins/navig-pipeline/…).
_MANIFEST_REL = ("../../../core/generated/commands.json", "../../../../core/generated/commands.json")


class DraftError(RuntimeError):
    """The draft could not be produced or did not survive its checks."""


@dataclass
class Draft:
    hook: dict[str, str]
    beats: list[dict[str, str]]
    intro: str
    outro: str
    posting: dict[str, Any]
    invalid_commands: list[str] = field(default_factory=list)
    unsourced_numbers: list[str] = field(default_factory=list)


def find_manifest(start: Path | None = None) -> Path | None:
    here = Path(start or __file__).resolve().parent
    for rel in _MANIFEST_REL:
        cand = (here / rel).resolve()
        if cand.is_file():
            return cand
    try:
        import navig  # the installed core ships its manifest beside the package in a checkout

        cand = Path(navig.__file__).resolve().parents[1] / "generated" / "commands.json"
        return cand if cand.is_file() else None
    except ImportError:
        return None


def command_paths(manifest: Path) -> set[str]:
    doc = json.loads(manifest.read_text(encoding="utf-8"))
    return {str(c.get("path", "")).strip() for c in doc.get("commands", []) if c.get("path")}


def resolves(command: str, paths: set[str]) -> bool:
    """True when the command's leading words name a real command path in the manifest."""
    words = command.split()
    if not words or words[0] != "navig":
        return False
    for n in range(len(words), 1, -1):
        if " ".join(words[:n]) in paths:
            return True
    return False


#: Shell alternation, placeholders and ellipses — documentation shapes, not a command a
#: capture can run literally (the first real draft offered `navig host lock status | acquire
#: | release` and `navig repo new agent-<n>`).
_NOT_LITERAL = re.compile(r"[|<>{}\[\]]|\.\.\.|…")
#: Leaf verbs that only read. Anything else may change state, and its capture is flagged.
READ_VERBS = {"status", "list", "ls", "show", "info", "check", "conflicts", "explain", "doctor", "stats",
              "report", "view", "search", "stale", "version", "--help", "-h", "--dry-run", "inspect", "audit"}


def capturable(command: str) -> bool:
    return not _NOT_LITERAL.search(command)


def read_only(command: str) -> bool:
    words = command.split()
    return any(w in READ_VERBS for w in words[1:])


def hashtag(tag: str) -> str:
    """`#ai-agents` → `#aiagents`: TikTok ends a hashtag at the first non-word character."""
    body = re.sub(r"[^\w]", "", tag.lstrip("#"))
    return f"#{body}" if body else ""


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in _NUMBER.findall(re.sub(r"`[^`]*`", " ", text or ""))}


PROMPT = """You turn a technical article into a 25-40 second vertical video script for TikTok.
Use ONLY facts and commands that appear in the ARTICLE below. Never invent a number, a result
or a command. Prefer claims the article marks as verified.

Return ONLY JSON:
{"hook": {"title": "<= 8 words, one *emphasised* phrase", "sub": "<= 8 words"},
 "intro": "one spoken sentence for the hook (<= 12 words)",
 "beats": [{"command": "navig ... (copied exactly from the article)",
            "caption": "<= 7 words, one *emphasised* word",
            "line": "one spoken sentence (<= 16 words)"}],
 "outro": "one short spoken sign-off (<= 8 words)",
 "posting": {"caption": "<= 2 sentences", "hashtags": ["#tag", "..."]}}
2 to 4 beats. Plain, concrete, slightly wry. No hype words.

ARTICLE:
"""


def draft(article: str, ask: Ask, paths: set[str] | None) -> Draft:
    # One retry: providers occasionally return an empty body or prose around the JSON
    # (seen on the second real draft of article 02) — a single failure is not a verdict.
    doc, raw = None, ""
    for prompt in (PROMPT + article[:24000],
                   "Your previous answer was not the JSON object requested. Return ONLY that JSON "
                   "object, nothing else.\n\n" + PROMPT + article[:24000]):
        raw = ask(prompt) or ""
        try:
            doc = json.loads(_FENCE.sub("", raw.strip()))
            break
        except ValueError:
            continue
    if not isinstance(doc, dict):
        raise DraftError(f"the model did not return JSON (twice): {raw[:200]!r}")
    beats = [b for b in doc.get("beats") or [] if isinstance(b, dict) and b.get("command")]
    if not beats:
        raise DraftError("the draft has no command beats")
    d = Draft(hook=doc.get("hook") or {}, beats=beats, intro=str(doc.get("intro") or ""),
              outro=str(doc.get("outro") or ""), posting=doc.get("posting") or {})
    if paths is not None:
        d.invalid_commands = [b["command"] for b in beats
                              if not resolves(b["command"], paths) or not capturable(b["command"])]
        d.beats = [b for b in beats if b["command"] not in d.invalid_commands]
    said = " ".join([d.hook.get("title", ""), d.hook.get("sub", ""), d.intro, d.outro,
                     d.posting.get("caption", ""), *(b.get("line", "") + " " + b.get("caption", "") for b in d.beats)])
    d.unsourced_numbers = sorted(_numbers(said) - _numbers(article))
    return d


def _slug(command: str) -> str:
    words = [w for w in command.split()[1:4] if re.fullmatch(r"[a-z][a-z0-9-]*", w)]
    return "-".join(words) or "cmd"


def write(d: Draft, out: Path, *, title: str, episode_id: str = "S01·E??") -> list[Path]:
    """Write scene.json, rebuild.json, posting.json and CAPTURE.md into ``out``."""
    if not d.beats:
        raise DraftError("no beat survived the command check — nothing to write")
    out.mkdir(parents=True, exist_ok=True)
    n = len(d.beats)
    casts = [f"{i + 1}-{_slug(b['command'])}.cast" for i, b in enumerate(d.beats)]
    scene = {"title": title, "episode": episode_id, "site": "navig.run", "segments": [
        {"type": "hook", "title": d.hook.get("title", title), "sub": d.hook.get("sub", ""), "art": "kraken"},
        *({"type": "terminal", "cast": casts[i], "command": b["command"], "title": f"~ {' '.join(b['command'].split()[:3])}",
           "step": f"{i + 1}/{n}", "caption": b.get("caption", ""), "pace": 6, "hold": 2.2}
          for i, b in enumerate(d.beats)),
        {"type": "outro", "cta": "pipx install navig", "follow": "Follow for one command a day"},
    ]}
    lines = [["n0", "daniel", d.intro, 0.6],
             *([f"n{i + 1}", "daniel", b.get("line", ""), 1.6] for i, b in enumerate(d.beats)),
             [f"n{n + 1}", "daniel", d.outro, 0.0]]
    plan = {"title": title, "bed": "deep-current", "length": 8.0 + 6.0 * n, "lang": "en",
            "lines": lines,
            "segs": [{"line": "n0", "hold": 0.4}, *({"line": f"n{i + 1}", "hold": 1.2} for i in range(n)), {}],
            "speech_map": [None, *range(1, n + 1), None], "updates": [],
            "scene": "scene.json", "clip": "clip-v2.md", "vo_dir": "_vo"}
    tags = [t for t in dict.fromkeys(hashtag(t) for t in ["#navig", *(d.posting.get("hashtags") or [])]) if t]
    posting = {"caption": d.posting.get("caption", ""), "hashtags": tags}
    capture = ["# Capture plan", "", "Record each beat ONCE, for real — the scene plays these casts.", "",
               *(f"{i + 1}. `navig pipeline cast -o {casts[i]} --cols 44 -- {b['command']}`"
                 + ("" if read_only(b["command"]) else "  ⚠ may change state — record against a sandbox "
                    "(`--config-dir sandbox`) or a throwaway target")
                 for i, b in enumerate(d.beats)),
               "", "Sensitive output → `--config-dir sandbox`; LAN addresses → `--redact VALUE=REPLACEMENT`.",
               "Then: `navig pipeline episode build .` (narrates, checks, renders, packs).", ""]
    if d.invalid_commands:
        capture += ["## Dropped — not a real navig command", "", *(f"- `{c}`" for c in d.invalid_commands), ""]
    if d.unsourced_numbers:
        capture += ["## Check before recording — numbers not found in the article", "",
                    *(f"- {x}" for x in d.unsourced_numbers), ""]
    written = []
    for name, body in (("scene.json", json.dumps(scene, indent=2, ensure_ascii=False)),
                       ("rebuild.json", json.dumps(plan, indent=2, ensure_ascii=False)),
                       ("posting.json", json.dumps(posting, indent=2, ensure_ascii=False)),
                       ("CAPTURE.md", "\n".join(capture))):
        (out / name).write_text(body + ("\n" if not body.endswith("\n") else ""), encoding="utf-8")
        written.append(out / name)
    return written


def model_ask() -> Ask:
    from navig_sdk.ai import AIError, generate

    def ask(prompt: str) -> str:
        try:
            return generate(prompt, temperature=0.4, mode="chat")
        except AIError as exc:
            raise DraftError(str(exc)) from exc

    return ask
