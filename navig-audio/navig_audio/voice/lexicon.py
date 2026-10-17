"""Pronunciation lexicon: how a name is SAID, per language, before synthesis.

TTS engines read brand words letter by letter or in the wrong language
("SCHEMA" in capitals came out spelled, or as a foreign word). The lexicon
rewrites only the SPOKEN text — whatever shows on screen keeps the original.

* Whole words only, case-insensitive: ``SCHEMA`` / ``Schema`` match,
  ``schematic`` does not.
* Longest term first, so ``schema.cx`` wins over ``schema``.
* Per language (``en``/``fr``/``ru``; a regional tag like ``fr-FR`` uses ``fr``).
* User entries from navig config ``audio.tts.lexicon.<lang>`` (a mapping
  term → spoken form) override the built-in ones.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

BUILTIN: dict[str, dict[str, str]] = {
    "en": {
        "SCHEMA": "Skeema",
        "schema.cx": "Skeema dot C X",
        "NAVIG": "Navig",
        "ARSH404": "Arsh four oh four",
    },
    "fr": {
        "SCHEMA": "schéma",
        "schema.cx": "schéma point C X",
        "NAVIG": "Navig",
        "ARSH404": "Arsh quatre cent quatre",
    },
    "ru": {
        "SCHEMA": "Схема",
        "schema.cx": "Схема точка си экс",
        "NAVIG": "Навиг",
        "ARSH404": "Арш четыреста четыре",
    },
}


def base_lang(lang: str | None) -> str:
    """``fr-FR`` → ``fr``; empty → ``""``."""
    return (lang or "").strip().lower().replace("_", "-").split("-")[0]


def user_entries(lang: str) -> dict[str, str]:
    """``audio.tts.lexicon.<lang>`` from navig config, or {} (standalone/unset)."""
    try:
        from navig.config import get as cfg_get  # lazy: runs without navig

        raw = cfg_get(f"audio.tts.lexicon.{lang}", None)
    except Exception:  # noqa: BLE001 — unreadable config never blocks speech
        return {}
    if not isinstance(raw, Mapping):
        return {}
    return {str(k): str(v) for k, v in raw.items() if str(k).strip()}


def entries(lang: str | None, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    code = base_lang(lang)
    table = dict(BUILTIN.get(code, {}))
    if code:
        table.update(user_entries(code))
    if extra:
        table.update(extra)
    return table


def apply(text: str, lang: str | None, extra: Mapping[str, str] | None = None) -> str:
    """The text as it should be spoken in ``lang``. Unknown language → unchanged."""
    table = entries(lang, extra)
    if not table or not text:
        return text
    terms = sorted(table, key=len, reverse=True)
    lookup = {t.lower(): table[t] for t in terms}
    # A term is a whole word: not glued to letters/digits, and not a piece of a
    # dotted name (so "schema" inside "schema.cx" is left to the longer term).
    pattern = re.compile(
        r"(?<![\w.])(" + "|".join(re.escape(t) for t in terms) + r")(?![\w]|\.\w)",
        re.IGNORECASE,
    )
    return pattern.sub(lambda m: lookup[m.group(1).lower()], text)
