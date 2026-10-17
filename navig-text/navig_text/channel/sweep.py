"""A literal terminology sweep across a channel's texts.

The wave1 migration note calls it the "cities→Zones sweep": after the world canon
settled, plain-English "city/cities" in citizen text became "Zone/Zones" (RU already
says Зона). The sweep is deliberately dumb: a table of exact replacements, applied
line by line to master entries (and optionally to the scripts), reported before
anything is written.

Rules live in ``<workspace>/channel.json`` under ``sweep``::

    {"sweep": {"rules": [{"find": "cities", "replace": "Zones"},
                          {"find": "city", "replace": "Zone", "whole_word": true}],
               "files": ["episodes.py", "transmission.py"]}}

With no config, the built-in default rules run against the master only. ``apply``
writes; the default only reports. Word-boundary matching keeps ``{citiesOpen}``,
``#citylife`` and ``city-progress.json`` out of the blast radius: the token contains
``cities`` but ``\\bcities\\b`` still matches inside ``{citiesOpen}`` — so token-shaped
segments (``{...}``, `#tags`, backtick code) are masked before matching and restored
after.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from navig_text.workspace import channel_config

__all__ = ["DEFAULT_RULES", "SweepResult", "sweep_config", "sweep_file", "sweep_rules"]


@dataclass(frozen=True)
class Rule:
    find: str
    replace: str
    whole_word: bool = True


#: Built-in default — the sweep the wave1 master was rewritten with.
DEFAULT_RULES: tuple[Rule, ...] = (
    Rule("cities", "Zones"),
    Rule("city", "Zone"),
    Rule("Cities", "Zones"),
    Rule("City", "Zone"),
)


@dataclass
class SweepResult:
    path: Path
    hits: list[tuple[int, str, str]] = field(default_factory=list)  # (line, before, after)
    applied: bool = False
    changed: bool = False

    def render(self) -> str:
        head = f"{self.path.name}: {len(self.hits)} hit(s)"
        if not self.hits:
            return f" {head}"
        rows = [f" {head}"]
        for ln, before, after in self.hits:
            rows.append(f"   {ln}: {before} → {after}")
        if self.changed and self.applied:
            rows.append(f"   wrote {self.path.name}")
        else:
            rows.append("   (dry)")
        return "\n".join(rows)


def sweep_rules(workspace: Path) -> list[Rule]:
    """Config from ``channel.json`` → the built-in defaults when absent."""
    cfg = channel_config(Path(workspace))
    raw = (cfg.get("sweep") or {}).get("rules")
    if not raw:
        return list(DEFAULT_RULES)
    out = []
    for r in raw:
        out.append(
            Rule(
                find=str(r["find"]),
                replace=str(r.get("replace", "")),
                whole_word=bool(r.get("whole_word", True)),
            )
        )
    return out


_MASK = re.compile(r"(\{[^}]*\}|#[\w]+|`[^`]*`)")
#: Citizen-facing lines: `- [en] …`, `- [ru] …`, `- [tx-013][en] …`. Everything
#: else (headers, rules, tables, beat scaffolding) is operator-facing — untouched.
_CONTENT = re.compile(r"^- \[(?:en|ru|fr|tx-\d+)\]")


def sweep_config(workspace: Path) -> tuple[list[Rule], str, tuple[str, ...]]:
    """(rules, scope, allow) from ``channel.json``.

    ``scope``: ``content`` (default — only citizen-facing lines) or ``all``.
    ``allow``: phrases whose line is skipped entirely — a canon name ("Living City"),
    a definition ("is a city") or a deliberate transition ("Cities became") are the
    workspace's calls, not this plugin's.
    """
    cfg = channel_config(Path(workspace)).get("sweep") or {}
    scope = str(cfg.get("scope") or "content")
    if scope not in ("content", "all"):
        scope = "content"
    allow = tuple(str(a) for a in (cfg.get("allow") or ()))
    return sweep_rules(workspace), scope, allow


def _apply_rules(line: str, rules: list[Rule]) -> tuple[str, bool]:
    """Apply rules to the UNMASKED parts of a line; returns (new_line, changed)."""
    parts = _MASK.split(line)
    changed = False
    for i, part in enumerate(parts):
        if i % 2 == 1:  # masked segment — tokens, tags, code: untouched
            continue
        new = part
        for rule in rules:
            pattern = (
                rf"\b{re.escape(rule.find)}\b" if rule.whole_word else re.escape(rule.find)
            )
            replaced = re.sub(pattern, rule.replace, new)
            if replaced != new:
                changed = True
                new = replaced
        parts[i] = new
    return "".join(parts), changed


def sweep_file(
    path: Path,
    rules: Optional[list[Rule]] = None,
    apply: bool = False,
    scope: str = "content",
    allow: tuple[str, ...] = (),
) -> SweepResult:
    """Sweep one text file. ``apply=False`` computes hits without writing.

    ``scope="content"`` touches only citizen-facing lines; a line containing any
    ``allow`` phrase is skipped entirely.
    """
    path = Path(path)
    rules = rules if rules is not None else list(DEFAULT_RULES)
    if not path.is_file():
        return SweepResult(path=path)
    text = path.read_text(encoding="utf-8")
    out_lines, result = [], SweepResult(path=path, applied=apply)
    for i, ln in enumerate(text.splitlines(), 1):
        if scope == "content" and not _CONTENT.match(ln):
            out_lines.append(ln)
            continue
        if allow and any(a in ln for a in allow):
            out_lines.append(ln)
            continue
        new, changed = _apply_rules(ln, rules)
        if changed:
            # report one row per line, showing the whole line before/after
            result.hits.append((i, ln.strip(), new.strip()))
        out_lines.append(new)
    new_text = "\n".join(out_lines) + ("\n" if text.endswith("\n") else "")
    if new_text != text:
        result.changed = True
        if apply:
            path.write_text(new_text, encoding="utf-8", newline="\n")
    return result
