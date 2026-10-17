"""A text's hook → a guide-demo sheet for ``navig audio beat demo``.

The hook is the first hook section of the chosen language (a pre-hook is skipped — it is a
pickup, not the part anyone hums). Who says each line comes from the text's own voices
table, a Markdown table under a heading that starts with ``Голоса`` / ``Voices`` / ``Voix``:

    | Lines | RU / EN | FR | Why |
    |---|---|---|---|
    | 1, 5 | `squad` | `squad` | … |
    | 2    | `monk`  | `monk-fr` | … |

The first column holds line numbers; a column whose header names a language code gives
that language's voice. A line the table does not cover takes ``default_voice``.
"""

from __future__ import annotations

import re
from typing import Any

from navig_text.lyrics.fit import HOOK_RE, LANG_HEAD_RE

PRE_RE = re.compile(r"ПРЕ-|PRÉ-|PRE-", re.I)
VOICES_HEAD_RE = re.compile(r"^##\s+(Голоса|Voices|Voix)\b", re.I)
LANG_CODE_RE = re.compile(r"\b(RU|EN|FR|UK)\b")
GUIDE_NOTE = "GUIDE, NOT FOR RELEASE"


class SheetError(ValueError):
    """A text a demo sheet cannot be built from."""


def hook_lines(text: str, lang: str) -> list[str]:
    """The first hook of ``lang``, stage directions dropped, bar marks turned into commas."""
    lang = lang.lower()
    current: str | None = None
    grab = False
    got: list[str] = []
    for raw in text.splitlines():
        s = raw.strip()
        m = LANG_HEAD_RE.match(s)
        if m:
            if got:
                break
            current, grab = m.group(1).lower(), False
            continue
        if s.startswith("## "):
            if got:
                break
            current, grab = None, False
            continue
        if s.startswith("### "):
            if got:
                break
            grab = current == lang and bool(HOOK_RE.search(s)) and not PRE_RE.search(s)
            continue
        if grab and s and s[0] not in "|>#-*_`[":
            line = re.sub(r"\(.*?\)", "", s)
            # A bar mark reads as a comma — unless punctuation already ends the phrase there.
            line = re.sub(r"([,.!?;:…—])\s*/\s*", r"\1 ", line)
            line = re.sub(r"\s*/\s*(?=[—–])", " ", line)
            line = re.sub(r"\s*/\s*", ", ", line)
            line = re.sub(r"\s+—\s*$", "", line).strip(" ,")
            if line:
                got.append(line)
    return got


def voice_table(text: str) -> dict[str, dict[int, str]]:
    """``{lang: {line_number: voice}}`` from the text's voices table."""
    out: dict[str, dict[int, str]] = {}
    in_section = False
    columns: list[list[str]] | None = None
    for raw in text.splitlines():
        s = raw.strip()
        if s.startswith("## "):
            in_section, columns = bool(VOICES_HEAD_RE.match(s)), None
            continue
        if not in_section or not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if columns is None:
            columns = [LANG_CODE_RE.findall(c.upper()) for c in cells]
            continue
        if all(set(c) <= set("-: ") for c in cells):
            continue  # the |---| rule
        numbers = [int(n) for n in re.findall(r"\d+", cells[0])]
        for i, langs in enumerate(columns):
            if i == 0 or i >= len(cells) or not langs:
                continue
            m = re.search(r"`([^`]+)`", cells[i]) or re.match(r"([\w-]+)", cells[i])
            if not m:
                continue
            for code in langs:
                for n in numbers:
                    out.setdefault(code.lower(), {})[n] = m.group(1)
    return out


def build_sheet(text: str, lang: str, beat: str, *, start_bar: int | None = None,
                start_block: str | None = None, bars_per_line: int = 1, bpm: float | None = None,
                default_voice: str | None = None, length_s: float = 30.0,
                lead_in_bars: int | None = None, max_lines: int | None = None) -> dict[str, Any]:
    """The demo sheet ``navig audio beat demo --sheet`` reads.

    The whole hook goes in by default: the demo itself drops any line that falls past
    ``length_s``, so a longer ``--length`` hears the lines a fixed cap would have cut.
    """
    if start_bar is None and not start_block:
        raise SheetError("say where the hook starts: start_bar or start_block")
    lines = hook_lines(text, lang)
    if max_lines is not None:
        lines = lines[:max_lines]
    if not lines:
        raise SheetError(f"no {lang.upper()} hook in this text (a '### [HOOK …]'-style heading under '## {lang.upper()}')")
    cast = voice_table(text).get(lang.lower(), {})
    missing = [i + 1 for i in range(len(lines)) if i + 1 not in cast and not default_voice]
    if missing:
        raise SheetError(f"no voice for hook line(s) {', '.join(map(str, missing))} — "
                         "add them to the voices table or pass a default voice")
    if lead_in_bars is None:
        lead_in_bars = 2 if (bpm and 240.0 / bpm * bars_per_line < 3.0) or not bpm else 1
    sheet: dict[str, Any] = {"beat": beat}
    if bpm:
        sheet["bpm"] = bpm
    sheet["language"] = lang.lower()
    if start_block:
        sheet["start_block"] = start_block
    else:
        sheet["start_bar"] = int(start_bar)  # type: ignore[arg-type]
    sheet.update({
        "lead_in_bars": lead_in_bars, "bars_per_line": bars_per_line, "length_s": length_s,
        "lines": [{"text": t, "voice": cast.get(i + 1, default_voice)} for i, t in enumerate(lines)],
    })
    return sheet


def dump(sheet: dict[str, Any], title: str) -> str:
    """YAML when PyYAML is present, JSON otherwise (JSON is valid YAML either way)."""
    head = f"# Guide-vocal demo — {title} · {GUIDE_NOTE}\n"
    try:
        import yaml
    except ImportError:  # pragma: no cover — PyYAML ships with navig
        import json

        return head + json.dumps(sheet, ensure_ascii=False, indent=2) + "\n"
    return head + yaml.safe_dump(sheet, allow_unicode=True, sort_keys=False, width=120)
