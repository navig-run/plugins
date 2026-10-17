"""Lint a channel master against the rules its own header declares.

The master's header says: *Rules that must survive a rewrite (they are enforced by
tests / the brand law)* — casing, no vendor names, balanced red-word asterisks,
digits on screen, real facts only. This module checks the mechanical half of those
rules, plus the structural invariants the old ``.tmp/fix_lore.py`` asserted with
``assert``: unique lore ids, beat counts matching the scripts, no empty lines where a
language is required.

A finding is (severity, where, message). ``error`` fails the run; ``note`` only
prints. Nothing here writes a byte.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from navig_text.channel.parse import ParsedMaster, parse_master
from navig_text.workspace import load_workspace_module

__all__ = ["Finding", "lint_master", "lint_scripts"]

#: Role words that stay lower case mid-sentence (owner rule, EN only — the RU/FR
#: equivalents are lower case by default and the glossary gate lives elsewhere).
ROLE_WORDS_EN = (
    "citizen", "founder", "pioneer", "mayor", "tourist", "steward", "treasurer",
    "mediator", "pathfinder", "harvester", "mentor", "anchor", "herald", "patron",
    "sentinel",
)

#: World terms that KEEP their capital (owner rule).
WORLD_TERMS_RU = (
    "Сетка", "Зона", "Паспорт", "Пульс", "Душа", "Герой", "Архитектор", "Энсина",
    "Кодекс", "Узел", "Агент", "Энсина",
)

#: Vendors/products that must never appear in citizen-facing text (the brand law's
#: sealed list, insofar as it is checkable mechanically). Schema's own guard is the
#: authority; this is the channel-side mirror so a master rewrite cannot smuggle one in.
VENDOR_NAMES = (
    "daily.dev", "cloudflare", "github", "gitlab", "openai", "chatgpt", "claude",
    "anthropic", "gemini", "deepseek", "elevenlabs", "replicate", "midjourney",
    "telegram", "youtube", "tiktok", "instagram", "telegram", "vk.com", "deepl",
    "hugging", "ollama", "openrouter",
)


@dataclass(frozen=True)
class Finding:
    severity: str  # "error" | "note"
    where: str     # e.g. "lore", "episodes:neighbour", "line 128"
    message: str

    def render(self) -> str:
        mark = "✗" if self.severity == "error" else "·"
        return f" {mark} [{self.where}] {self.message}"


# ── master-internal checks ────────────────────────────────────────────────────
def lint_master(
    path: Path, parsed: Optional[ParsedMaster] = None, strict: bool = False
) -> list[Finding]:
    """Rules that live entirely inside the master file + its scripts' shape."""
    findings: list[Finding] = []
    try:
        m = parsed or parse_master(path)
    except Exception as exc:
        return [Finding("error", "parse", str(exc))]

    # 1. unique lore ids — checked on the RAW text: parse_master folds duplicates
    #    into the dict (last wins), so the parsed view can never show a double.
    raw = Path(path).read_text(encoding="utf-8").replace("\r", "")
    raw_ids = re.findall(r"^\*\*lore-(\S+)\*\*", raw, re.M)
    dupes = sorted({i for i in raw_ids if raw_ids.count(i) > 1})
    if dupes:
        findings.append(
            Finding("error", "lore", f"duplicate lore ids in master: {', '.join(dupes)}")
        )
    if not raw_ids:
        findings.append(Finding("error", "lore", "no lore entries in master"))

    # 2. every episode has beats, every beat has at least en
    for name, ep in m.episodes.items():
        if not ep["beats"]:
            findings.append(Finding("error", f"episodes:{name}", "no beats parsed"))
        for i, beat in enumerate(ep["beats"], 1):
            if "en" not in beat["lines"]:
                findings.append(
                    Finding("error", f"episodes:{name}", f"beat {i} has no [en] line")
                )

    # 3. transmissions have a head and matching section sets per language
    for pid, tx in m.tx.items():
        for lang in ("en", "ru"):
            block = tx.get(lang) or {}
            if not block.get("head"):
                findings.append(
                    Finding("error", f"tx:{pid}", f"{lang} has no [head] line")
                )

    # 4. asterisk balance — *word* marks the one red word on screen; an unpaired
    #    asterisk bleeds red across the rest of the line.
    findings.extend(_asterisk_findings(path))

    # 5. casing + vendor names on the lines that reach a citizen
    findings.extend(_content_findings(path, strict=strict))
    return findings


def _content_lines(path: Path) -> list[tuple[int, str]]:
    """Only the lines a citizen reads: ``- [lang] …`` master entries."""
    out = []
    for i, ln in enumerate(
        Path(path).read_text(encoding="utf-8").replace("\r", "").splitlines(), 1
    ):
        if re.match(r"^- \[(en|ru|fr)\]", ln):
            out.append((i, ln))
    return out


def _asterisk_findings(path: Path) -> list[Finding]:
    findings = []
    for i, ln in _content_lines(path):
        # strip literal ** bold and code spans before counting red-markers
        stripped = ln.replace("**", "").replace("`", "")
        if stripped.count("*") % 2:
            findings.append(
                Finding("error", f"line {i}", "unpaired * (red-word marker)")
            )
    return findings


#: Hashtags, tokens and code spans are not prose — casing rules never apply to them
#: (`#сетка` is a tag, `{citiesOpen}` is a token, `` `code` `` is code).
_NON_PROSE = re.compile(r"(#[\w]+|\{[^}]*\}|`[^`]*`)")


def _content_findings(path: Path, strict: bool = False) -> list[Finding]:
    findings = []
    for i, ln in _content_lines(path):
        body = re.sub(r"^- \[(en|ru|fr)\]", "", ln)
        # vendor check reads the raw line (a vendor name hides in no tag)
        low = body.lower()
        for name in VENDOR_NAMES:
            if name in low:
                findings.append(
                    Finding("error", f"line {i}", f"vendor name in citizen text: {name!r}")
                )
        prose = _NON_PROSE.sub(" ", body)
        # EN role words capitalized mid-sentence (title-before-name is allowed:
        # "Mayor Alice" — flag only when NOT followed by a proper name)
        if "- [en]" in ln:
            for role in ROLE_WORDS_EN:
                for m_ in re.finditer(rf"\b({role[0].upper() + role[1:]})\b", prose):
                    after = prose[m_.end():].lstrip()
                    if after and not after[0].isupper():
                        findings.append(
                            Finding(
                                "error" if strict else "note",
                                f"line {i}",
                                f"capitalized role word mid-sentence: {m_.group(1)}",
                            )
                        )
        # RU world terms must keep their capital — flag a lower-case one
        if "- [ru]" in ln:
            for term in WORLD_TERMS_RU:
                if re.search(rf"\b{term.lower()}\b", prose):
                    findings.append(
                        Finding("error", f"line {i}", f"world term lost its capital: {term}")
                    )
    return findings


# ── script-side checks (master vs the channel's own scripts) ─────────────────
def lint_scripts(workspace: Path, m: ParsedMaster) -> list[Finding]:
    """Structural agreement between the master and the scripts it feeds."""
    findings: list[Finding] = []
    ws = Path(workspace)

    try:
        EP = load_workspace_module(ws, "episodes")
    except Exception as exc:
        return [Finding("error", "episodes", f"cannot import episodes.py: {exc}")]

    for name, data in EP.EPISODES.items():
        rd = m.episodes.get(name)
        if rd is None:
            findings.append(
                Finding("error", f"episodes:{name}", "episode missing from master")
            )
            continue
        if len(rd["beats"]) != len(data["beats"]):
            findings.append(
                Finding(
                    "error",
                    f"episodes:{name}",
                    f"beat count: scripts have {len(data['beats'])},"
                    f" master has {len(rd['beats'])}",
                )
            )

    # story cards — the master's ## 8 ARCHIVE CARDS vs story_kit.STORIES.
    # story_kit is not in sync's patch set, so this pair drifted silently
    # once already (the sweep's hand-fix reached one file only); without
    # this check both gates stay green over a disagreeing card.
    try:
        SK = load_workspace_module(ws, "story_kit")
    except Exception as exc:
        findings.append(Finding("error", "cards", f"cannot import story_kit.py: {exc}"))
    else:
        kit = {
            s.get("id"): s
            for s in getattr(SK, "STORIES", [])
            if isinstance(s, dict) and s.get("id")
        }
        pairs = (
            ("en", "head", "head"),
            ("en", "sub", "sub"),
            ("ru", "head", "ru_head"),
            ("ru", "sub", "ru_sub"),
        )
        for sid, block in (m.get("cards") or {}).items():
            spec = kit.get(sid)
            if spec is None:
                findings.append(
                    Finding(
                        "error",
                        f"cards:ST-{sid}",
                        "in the master but not in story_kit.py",
                    )
                )
                continue
            for lang, mk, sk_key in pairs:
                mv = (block.get(lang) or {}).get(mk)
                if mv is None:
                    continue
                if spec.get(sk_key) != mv:
                    findings.append(
                        Finding(
                            "error",
                            f"cards:ST-{sid} [{lang}][{mk}]",
                            f"master and story_kit.py disagree on {sk_key!r} —"
                            " reconcile by hand (story_kit is not in the sync patch set)",
                        )
                    )
        for sid in kit:
            if sid not in (m.get("cards") or {}):
                findings.append(
                    Finding(
                        "error",
                        f"cards:ST-{sid}",
                        "in story_kit.py but not in the master",
                    )
                )

    try:
        LT = load_workspace_module(ws, "lore_texts")
    except Exception as exc:
        findings.append(Finding("error", "lore", f"cannot import lore_texts.py: {exc}"))
        return findings

    ids = []
    for i, e in enumerate(LT.LORE):
        if not isinstance(e, dict):
            findings.append(
                Finding(
                    "error",
                    "lore",
                    f"lore_texts.py entry {i} is {type(e).__name__}, not a dict —"
                    " corrupted (nested list); rebuild with channel sync after fixing",
                )
            )
            continue
        if "id" not in e:
            findings.append(Finding("error", "lore", f"lore_texts.py entry {i} has no id"))
            continue
        ids.append(e["id"])
    if len(ids) != len(set(ids)):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        findings.append(
            Finding("error", "lore", f"duplicate ids in lore_texts.py: {', '.join(dupes)}")
        )
    if ids and ids[0] != "ensina-desk":
        findings.append(
            Finding("note", "lore", f"first lore entry is {ids[0]!r}, canon says ensina-desk")
        )
    for lid in m.lore:
        if lid not in ids:
            findings.append(
                Finding("error", "lore", f"master entry {lid!r} not in lore_texts.py")
            )
    return findings
