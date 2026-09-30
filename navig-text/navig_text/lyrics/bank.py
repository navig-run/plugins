"""A word bank: find an entry, find a rhyme.

A word bank is any folder of Markdown notes — one word, phrase or collocation per file,
the way a Notion export leaves them. ``search`` finds the entries that mention a term;
``rhymes`` collects every word used across the bank (and any texts you point it at) and
ranks the ones whose ending matches.

Rhymes are matched on **spelling**, not sound. *Strong*: the same tail from the
second-to-last vowel group (района / закона, remember / december). *Weak*: the same last
vowel group and what follows it (night / light) — and when the word ends on that vowel,
the consonant before it too (стена / луна, not стена / душа). French silent endings
(-e, -es, -s, -t, -x, -ent) are dropped first, so ronde / monde / rondes meet. It is a
finder for a human ear, not a judge: it cannot hear that ville and fil rhyme.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from navig_text.lyrics.fit import LAT_VOWELS, RU_VOWELS, detect_language

WORD_RE = re.compile(r"[^\W\d_]+(?:['’-][^\W\d_]+)*", re.U)
SKIP_NAMES = {"readme.md", "index.md", "catalog.md"}


@dataclass
class Entry:
    path: Path
    title: str
    snippet: str


def _files(dirs: list[Path]) -> list[Path]:
    out: list[Path] = []
    for d in dirs:
        if d.is_file():
            out.append(d)
        elif d.is_dir():
            out += sorted(p for p in d.rglob("*.md") if p.name.lower() not in SKIP_NAMES)
    return out


def _title(path: Path, text: str) -> str:
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("# "):
            # A Notion export titles a one-cell page as a table row: "# | the phrase |".
            return s[2:].strip().strip("|").strip() or path.stem
    return path.stem


def search(dirs: list[Path], query: str, *, limit: int = 30) -> list[Entry]:
    """Entries whose title or body mention ``query`` (case-insensitive), title hits first."""
    q = query.casefold().strip()
    if not q:
        return []
    title_hits: list[Entry] = []
    body_hits: list[Entry] = []
    for p in _files(dirs):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        title = _title(p, text)
        if q not in text.casefold() and q not in p.stem.casefold():
            continue
        snippet = next((ln.strip() for ln in text.splitlines()
                        if q in ln.casefold() and not ln.strip().startswith("#")), "")
        e = Entry(path=p, title=title, snippet=snippet[:160])
        (title_hits if q in title.casefold() or q in p.stem.casefold() else body_hits).append(e)
    return (title_hits + body_hits)[:limit]


def _vowel_groups(word: str) -> list[tuple[int, int]]:
    vowels = RU_VOWELS if any(c in RU_VOWELS for c in word) else set(LAT_VOWELS)
    groups: list[tuple[int, int]] = []
    i = 0
    while i < len(word):
        if word[i] in vowels:
            j = i
            while j + 1 < len(word) and word[j + 1] in vowels:
                j += 1
            groups.append((i, j))
            i = j + 1
        else:
            i += 1
    return groups


def _sounding(word: str, lang: str) -> str:
    w = word.casefold().replace("’", "'")
    if lang == "fr":
        for end in ("ent", "es", "e", "s", "t", "x"):
            if w.endswith(end) and len(w) - len(end) >= 2 and _vowel_groups(w[: -len(end)]):
                w = w[: -len(end)]
                break
    return w


def tails(word: str, lang: str) -> tuple[str, str]:
    """(strong, weak) rhyme tails of ``word``; strong is empty for a one-vowel word."""
    w = _sounding(word, lang)
    groups = _vowel_groups(w)
    if not groups:
        return "", ""
    start, end = groups[-1]
    weak = w[start:]
    if end == len(w) - 1 and start > 0 and w[start - 1] not in "'-":
        weak = w[start - 1:]  # an open last syllable leans on the consonant before it
    strong = w[groups[-2][0]:] if len(groups) > 1 else ""
    return strong, weak


def vocabulary(dirs: list[Path]) -> Counter[str]:
    """Every word (3+ letters) used across the files under ``dirs``, with its count."""
    words: Counter[str] = Counter()
    for p in _files(dirs):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        words.update(w.casefold() for w in WORD_RE.findall(text) if len(w) >= 3)
    return words


def rhymes(word: str, vocab: Counter[str], *, lang: str | None = None,
           limit: int = 40) -> list[tuple[str, str, int]]:
    """``(candidate, "strong"|"weak", uses)`` ranked strong first, then by how often it is used."""
    lang = lang or detect_language(word)
    target = word.casefold()
    strong, weak = tails(target, lang)
    if not weak:
        return []
    cyr = any(c in RU_VOWELS for c in target)
    hits: list[tuple[int, int, str, str]] = []
    for cand, n in vocab.items():
        if cand == target or cyr != any(c in RU_VOWELS for c in cand):
            continue
        cs, cw = tails(cand, lang)
        if strong and cs == strong:
            hits.append((0, -n, cand, "strong"))
        elif cw == weak:
            hits.append((1, -n, cand, "weak"))
    hits.sort()
    return [(c, kind, -neg) for _, neg, c, kind in hits[:limit]]
