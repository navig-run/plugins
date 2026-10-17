"""Parse a channel master text (``ALL-TEXTS*.md``) into structured sections.

Ported from the workspace's ``.tmp/sync_rev6.py`` — positional by structure, never
by text generation: the file is a set of numbered ``## N ·`` sections, each with its
own line shapes. Parsing is pure: it reads bytes, returns data, touches nothing.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

__all__ = ["MasterError", "ParsedMaster", "md_lines", "parse_master"]


class MasterError(ValueError):
    """The master file is missing or not a master we recognize."""


def md_lines(t: str) -> str:
    """``foo ⏎ bar`` (the master's inline hard-wrap) → a real newline."""
    return re.sub(r" *⏎ *", "\n", t)


class ParsedMaster(dict):
    """dict with the master's parsed sections as keys, plus ``path`` metadata.

    Keys: ``episodes`` (name → {end, beats}), ``tx`` (id → {en, ru}), ``lore``,
    ``posts``, ``txlines``, ``cards`` (ST id → {en, ru} → {head, sub}).
    A plain dict subclass so tests can compare directly.
    """

    @property
    def episodes(self) -> dict:
        return self["episodes"]

    @property
    def tx(self) -> dict:
        return self["tx"]

    @property
    def lore(self) -> dict:
        return self["lore"]

    @property
    def posts(self) -> dict:
        return self["posts"]

    @property
    def txlines(self) -> dict:
        return self["txlines"]

    @property
    def cards(self) -> dict:
        return self["cards"]


def parse_master(path: Path) -> ParsedMaster:
    """Read the master file at ``path``. Raises :class:`MasterError` if unreadable."""
    path = Path(path)
    if not path.is_file():
        raise MasterError(f"master text not found: {path}")
    try:
        text = path.read_text(encoding="utf-8").replace("\r", "")
    except OSError as exc:
        raise MasterError(f"cannot read master text {path}: {exc}") from exc
    if "## 1 ·" not in text:
        raise MasterError(
            f"{path.name} does not look like a channel master (no '## 1 ·' section)"
        )

    out = ParsedMaster(
        {"episodes": {}, "tx": {}, "lore": {}, "posts": {}, "txlines": {},
         "cards": {}}
    )
    sec: Optional[str] = None
    cur_ep = cur_beat = cur_tx = cur_post = cur_st = None
    for ln in text.splitlines():
        if ln.startswith("## 1 ·"):
            sec = "ep"
        elif ln.startswith("## 2 ·"):
            sec = "tx"
        elif ln.startswith("## 3 ·"):
            sec = "lore"
        elif ln.startswith("## 4 ·"):
            sec = "posts"
        elif ln.startswith("## 5 ·"):
            sec = "txl"
        elif ln.startswith("## 6 ·"):
            sec = None
        elif re.match(r"^## \d+ · ARCHIVE CARDS", ln):
            # §8 — the story cards story_kit.py renders; matched on the title,
            # not the number, so renumbering the sections cannot hide it.
            sec = "cards"
        elif ln.startswith("## "):
            # any other section header (7 pronunciation, 9 formats, …)
            sec = None
        if sec == "ep":
            m = re.match(r"^### (\S+) — TX", ln)
            if m:
                cur_ep = m.group(1)
                out["episodes"][cur_ep] = {"end": {}, "beats": []}
                em = re.findall(
                    r"(en|ru|fr) [“\"](.+?)[”\"]", ln.split("end card:")[-1]
                )
                for k, v in em:
                    out["episodes"][cur_ep]["end"][k] = v
                continue
            m = re.match(r"^\*\*(\S+)-beat-(\d+)\*\*", ln)
            if m and cur_ep:
                cur_beat = {"lines": {}}
                out["episodes"][cur_ep]["beats"].append(cur_beat)
                continue
            m = re.match(r"^- \[(en|ru|fr)\] ?(.*)$", ln)
            if m and cur_beat is not None:
                cur_beat["lines"][m.group(1)] = m.group(2)
        elif sec == "tx":
            m = re.match(r"^### transmission-(\d+)", ln)
            if m:
                cur_tx = m.group(1)
                out["tx"][cur_tx] = {
                    "en": {"head": None, "sections": []},
                    "ru": {"head": None, "sections": []},
                }
                continue
            m = re.match(r"^- \[(en|ru)\]\[(head|[^\]]+)\] ?(.*)$", ln)
            if m and cur_tx:
                lang, label, body = m.groups()
                if label == "head":
                    out["tx"][cur_tx][lang]["head"] = body
                else:
                    out["tx"][cur_tx][lang]["sections"].append((label, body))
        elif sec == "lore":
            m = re.match(r"^\*\*lore-(\S+)\*\*", ln)
            if m:
                cur_post = m.group(1)
                out["lore"][cur_post] = {}
            m = re.match(r"^- \[(en|ru)\] ?(.*)$", ln)
            if m and cur_post:
                out["lore"][cur_post][m.group(1)] = md_lines(m.group(2))
        elif sec == "posts":
            m = re.match(r"^\*\*post-(\S+)\*\*", ln)
            if m:
                cur_post = m.group(1)
                out["posts"][cur_post] = {}
            m = re.match(
                r"^- \[(en|ru|fr)\]\[(title|caption|pin|tags)\] ?(.*)$", ln
            )
            if m and cur_post:
                out["posts"][cur_post].setdefault(m.group(2), {})[m.group(1)] = m.group(3)
        elif sec == "txl":
            m = re.match(r"^- \[tx-(\d+)\]\[(en|ru)\] ?(.*)$", ln)
            if m:
                out["txlines"].setdefault(m.group(1), {})[m.group(2)] = m.group(3)
        elif sec == "cards":
            m = re.match(r"^\*\*ST-(\S+)\*\*", ln)
            if m:
                cur_st = m.group(1)
                out["cards"][cur_st] = {"en": {}, "ru": {}}
                continue
            m = re.match(r"^- \[(en|ru)\]\[(head|sub)\] ?(.*)$", ln)
            if m and cur_st is not None:
                out["cards"][cur_st][m.group(1)][m.group(2)] = m.group(3)
    return out
