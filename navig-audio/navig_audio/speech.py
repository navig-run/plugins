"""What the narrator SAYS, separate from what the screen SHOWS.

The screen shows "266"; a Russian narrator must say "двести шестьдесят
шесть", and "1 из 5" must be "один из пяти" (genitive), and "в 2021 году"
must be "в две тысячи двадцать первом году" (prepositional ordinal). Left to
itself the voice model guessed — and guessed wrong in Russian and French.

:func:`groups` maps every DISPLAY token to the spoken words that stand for it,
so a kinetic renderer can send the spoken text to the voice and still land
"266" on screen at the moment the narrator says the whole number.

:func:`narration` is the flat version for the transmissions, where the copy
also carries coordinates and a station number.

Ported verbatim from the channel workspace's ``speech.py`` — the RU case table
is copy-driven and must stay byte-compatible with what was tuned by ear.
"""

from __future__ import annotations

import re

from num2words import num2words

__all__ = ["RU_GEN", "STATION", "groups", "narration"]

_PUNCT = ".,:;!?»«—–\"'()"

# genitive cardinals for "N из M" (one of five), the only case the copy needs
RU_GEN = {1: "одного", 2: "двух", 3: "трёх", 4: "четырёх", 5: "пяти", 6: "шести",
          7: "семи", 8: "восьми", 9: "девяти", 10: "десяти", 11: "одиннадцати",
          12: "двенадцати", 15: "пятнадцати", 20: "двадцати", 25: "двадцати пяти"}

STATION = {"en": "thirteen-oh-ten", "ru": "тринадцать-ноль-десять",
           "fr": "treize-zéro-dix"}


def _split(tok: str) -> tuple[str, str, str]:
    """('«', '266', '».') — leading punct, core, trailing punct."""
    a = len(tok) - len(tok.lstrip(_PUNCT))
    b = len(tok.rstrip(_PUNCT))
    return tok[:a], tok[a:b], tok[b:]


def _ru_gen_ordinal(n: int) -> str:
    """'в марте 2021 года' -> 'две тысячи двадцать первого года'."""
    words = num2words(n, lang="ru", to="ordinal").split()
    last = words[-1]
    if last == "третий":
        last = "третьего"
    elif last.endswith("ий"):
        last = last[:-2] + "его"
    elif last.endswith(("ый", "ой")):
        last = last[:-2] + "ого"
    return " ".join(words[:-1] + [last])


def _ru_prep_ordinal(n: int) -> str:
    words = num2words(n, lang="ru", to="ordinal").split()
    last = words[-1]
    if last.endswith("ий") and last != "третий":
        last = last[:-2] + "ем"
    elif last == "третий":
        last = "третьем"
    elif last.endswith(("ый", "ой")):
        last = last[:-2] + "ом"
    return " ".join(words[:-1] + [last])


def _num(core: str, lang: str, prev: str, nxt: str) -> list[str] | None:
    if not core.isdigit():
        return None
    n = int(core)
    if core == "13010":
        return STATION.get(lang, core).split()
    if lang == "ru":
        p, x = prev.lower().strip(_PUNCT), nxt.lower().strip(_PUNCT)
        if x == "года":
            return _ru_gen_ordinal(n).split()
        if p in ("в", "во") and x.startswith("год"):
            return _ru_prep_ordinal(n).split()
        if p == "из" and n in RU_GEN:
            return RU_GEN[n].split()
        return num2words(n, lang="ru").split()
    if lang == "fr":
        if 1900 <= n <= 2099:
            return num2words(n, lang="fr", to="year").split()
        return num2words(n, lang="fr").split()
    return None                                # English: the model reads digits well


def groups(tokens: list[str], lang: str) -> list[list[str]]:
    out = []
    for i, tok in enumerate(tokens):
        lead, core, trail = _split(tok)
        prev = tokens[i - 1] if i else ""
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        words = _num(core, lang, prev, nxt)
        if words is None:
            out.append([tok])
        else:
            words[0] = lead + words[0]
            words[-1] = words[-1] + trail
            out.append(words)
    return out


_COORDS = {
    "en": "zero degrees north, zero degrees east",
    "ru": "ноль градусов северной широты, ноль градусов восточной долготы",
    "fr": "zéro degré nord, zéro degré est",
}


def narration(text: str, lang: str) -> str:
    """Flat spoken form for a whole paragraph (transmissions)."""
    text = text.replace("*", "")
    say = _COORDS.get(lang, _COORDS["en"])
    # a Russian "в. д." ends with its own full stop, which is also the
    # sentence's — keep one when the abbreviation is consumed
    text = re.sub(r"0°\s?00['′]\s?(N|с\.\s?ш\.)\s+0°\s?00['′]\s?(E|в\.\s?д\.)",
                  lambda m: say + ("." if m.group(0).endswith(".") else ""), text)
    toks = text.split(" ")
    return " ".join(w for g in groups(toks, lang) for w in g)
