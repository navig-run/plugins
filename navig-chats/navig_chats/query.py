"""Turn what a person types into a safe FTS5 expression.

Supported syntax, nothing more:

* ``word`` — a term; ``word*`` — a prefix.
* ``"exact phrase"`` — a phrase.
* ``-word`` / ``-"a phrase"`` / ``NOT word`` — exclude.
* ``a OR b`` — either; terms side by side mean AND.

Every term is re-quoted before it reaches ``MATCH``, so FTS5 syntax characters a person did
not mean as syntax (``:``, ``(``, ``^``, ``-`` inside a word, a stray quote) can never raise
or change what the query means. A query made only of exclusions has nothing to rank and is
rejected as :class:`EmptyQuery`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_TOKEN = re.compile(r'(-?)"([^"]*)"?|(\S+)')


class EmptyQuery(ValueError):
    """The query has no positive term (empty, or exclusions only)."""


@dataclass
class ParsedQuery:
    expression: str
    terms: list[str] = field(default_factory=list)  # positive words/phrases, for highlighting


def _quote(text: str, prefix: bool = False) -> str:
    return '"' + text.replace('"', '""') + '"' + ("*" if prefix else "")


def parse(raw: str) -> ParsedQuery:
    """Parse ``raw`` into an FTS5 MATCH expression. Raises :class:`EmptyQuery`."""
    positives: list[str] = []  # already-quoted terms, with literal "OR" between alternatives
    negatives: list[str] = []
    terms: list[str] = []
    negate_next = False
    for m in _TOKEN.finditer(raw or ""):
        neg_mark, phrase, word = m.group(1), m.group(2), m.group(3)
        if phrase is not None:
            words = phrase.split()
            if not words:
                continue
            quoted = _quote(" ".join(words))
            is_neg = bool(neg_mark) or negate_next
            negate_next = False
            if is_neg:
                negatives.append(quoted)
            else:
                positives.append(quoted)
                terms.append(" ".join(words))
            continue
        if word in ("OR", "|"):
            if positives and positives[-1] != "OR":
                positives.append("OR")
            continue
        if word in ("AND", "&&"):
            continue
        if word == "NOT":
            negate_next = True
            continue
        is_neg = negate_next
        negate_next = False
        if word.startswith("-") and len(word) > 1:
            is_neg, word = True, word[1:]
        prefix = word.endswith("*") and len(word) > 1
        core = word.rstrip("*")
        # Keep letters/digits; anything else splits the word (FTS5's tokenizer would anyway).
        parts = [p for p in re.split(r"[^\w]+", core) if p]
        if not parts:
            continue
        quoted = _quote(" ".join(parts), prefix=prefix and len(parts) == 1)
        if is_neg:
            negatives.append(quoted)
        else:
            positives.append(quoted)
            terms.append(" ".join(parts))
    while positives and positives[-1] == "OR":
        positives.pop()
    while positives and positives[0] == "OR":
        positives.pop(0)
    if not positives:
        raise EmptyQuery(raw)
    expr = " ".join(positives)
    if negatives:
        expr = f"({expr}) NOT ({' OR '.join(negatives)})"
    return ParsedQuery(expression=expr, terms=terms)


__all__ = ["EmptyQuery", "ParsedQuery", "parse"]
