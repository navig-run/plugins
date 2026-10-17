"""Repair filename damage, then slug. Pure functions, no I/O.

Real archives accumulate encoding damage that a naive migration bakes in forever.
This corpus carries four distinct kinds, each from a different tool:

* ``DECLARATION+DE+DOMICILE+Ukraine+pieces.pdf`` — a browser saved a URL-encoded name.
* ``Cybesis Studios (7_10_2025 12：06：31 AM).txt`` — a full-width colon, substituted
  because ``:`` is illegal in a Windows filename.
* ``dйclaration de CA_T3_2022.pdf`` — UTF-8 bytes read back as cp1251 (Cyrillic).
* ``CONDITIONS GEěNEěRALES D'UTILISATION .pdf`` — combining acute marks detached and
  rendered as ``ě`` after the vowel they belonged to.

Every repair is **guarded**: it applies only when it demonstrably improves the string,
so a name that is already correct passes through untouched. The mojibake guards count
French accented characters before and after and keep the result only if the count
rises — which is what stops ``repair()`` from mangling a legitimately Cyrillic
filename such as ``СВИДЕТЕЛЬСТВО О РОЖДЕНИИ.pdf``.

The unrepaired original is always carried alongside in the plan row and the ledger,
so nothing here is lossy.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import unquote_plus

# Characters whose presence means a cp1252/cp1251 round-trip actually recovered text.
_FRENCH_ACCENTS = set("àâäéèêëîïôöùûüçœÀÂÄÉÈÊËÎÏÔÖÙÛÜÇŒ")

# A trailing "(1" with no closing paren — `Attestation_de_tiers_payant (1.pdf`.
_UNCLOSED_PAREN = re.compile(r"\s*\(\d+$")
# A trailing duplicate marker Windows/browsers add — ` (1)`, `(2)`.
_DUP_MARKER = re.compile(r"\s*\((\d+)\)$")
_NON_SLUG = re.compile(r"[^a-z0-9]+")
_MULTI_DASH = re.compile(r"-{2,}")

# Suffix tokens stripped when grouping near-duplicates. Deliberately NOT stripped by
# `repair()` — they are part of the real name until a human says otherwise.
_NEAR_DUP_TOKENS = ("_compressed", "-compressed", "_valide", "_v1", "_v2", "-1-8", "-1")

MAX_STEM = 120

# Cyrillic -> Latin, so a Russian filename survives slugging as a readable word instead
# of vanishing. Longer sequences first: `щ` must be consumed before `ш`.
_CYRILLIC: tuple[tuple[str, str], ...] = (
    ("щ", "shch"), ("ш", "sh"), ("ч", "ch"), ("ц", "ts"), ("ж", "zh"),
    ("ю", "yu"), ("я", "ya"), ("ё", "yo"), ("э", "e"), ("ы", "y"),
    ("а", "a"), ("б", "b"), ("в", "v"), ("г", "g"), ("д", "d"), ("е", "e"),
    ("з", "z"), ("и", "i"), ("й", "j"), ("к", "k"), ("л", "l"), ("м", "m"),
    ("н", "n"), ("о", "o"), ("п", "p"), ("р", "r"), ("с", "s"), ("т", "t"),
    ("у", "u"), ("ф", "f"), ("х", "h"), ("ъ", ""), ("ь", ""),
    # Ukrainian / Belarusian letters that appear in this corpus.
    ("і", "i"), ("ї", "yi"), ("є", "ye"), ("ґ", "g"), ("ў", "u"),
)


def _transliterate(text: str) -> str:
    """Romanise Cyrillic. Anything else is returned untouched."""
    if not any("Ѐ" <= c <= "ӿ" for c in text):
        return text
    out: list[str] = []
    for ch in text:
        lower = ch.lower()
        for cyr, lat in _CYRILLIC:
            if lower == cyr:
                out.append(lat.upper() if ch.isupper() and lat else lat)
                break
        else:
            out.append(ch)
    return "".join(out)


def _accent_count(s: str) -> int:
    return sum(1 for c in s if c in _FRENCH_ACCENTS)


def _is_cyrillic(c: str) -> bool:
    return "Ѐ" <= c <= "ӿ"


def _looks_like_damage(s: str, enc_from: str) -> bool:
    """Is *s* plausibly mojibake rather than legitimate non-ASCII text?

    An accent-count guard alone is not enough, and getting this wrong is destructive:
    ``СВИДЕТЕЛЬСТВО О РОЖДЕНИИ`` (a birth certificate) round-trips through cp1251 into
    ``ÑÂÈÄÅÒÅËÜÑÒÂÎ Î ÐÎÆÄÅÍÈÈ``, which *raises* the French-accent count and so passes
    a naive check — silently destroying a real filename.

    The discriminator is that mojibake is **mixed**: cp1251 damage puts a few Cyrillic
    characters inside otherwise-Latin words (``dйclaration``), whereas genuine Cyrillic
    text is Cyrillic throughout. cp1252 damage leaves its own signature bigrams.
    """
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return False
    if enc_from == "cp1251":
        cyr = sum(1 for c in letters if _is_cyrillic(c))
        latin = len(letters) - cyr
        # Damage: a Latin-dominant string with a sprinkling of Cyrillic.
        return 0 < cyr <= len(letters) * 0.3 and latin > 0
    # cp1252 damage always leaves an "Ã"/"Â"/"â€" sequence behind.
    return any(marker in s for marker in ("Ã", "Â", "â€", " Å", "Ð"))


def _try_recode(s: str, enc_from: str, enc_to: str) -> str:
    """Round-trip *s* through a mis-decoding, keeping it only if it is real damage.

    Two guards, both required: the string must *look* like damage from this specific
    encoding, and the result must contain more French accents than the input. A clean
    ASCII name fails the first, genuine Cyrillic fails the first, and a false positive
    that produces no accents fails the second.
    """
    if not _looks_like_damage(s, enc_from):
        return s
    try:
        candidate = s.encode(enc_from, errors="strict").decode(enc_to, errors="strict")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s
    return candidate if _accent_count(candidate) > _accent_count(s) else s


def _fix_detached_marks(s: str) -> str:
    """``GEěNEěRALES`` -> ``GENERALES``.

    A caron-bearing latin letter directly after a vowel is not a French word; it is a
    combining mark that lost its base. Drop the intruder, then strip anything left
    over after NFKD so the stem is plain.
    """
    if not any(c in s for c in "ěĚ"):
        return s
    out = []
    for i, ch in enumerate(s):
        if ch in "ěĚ" and i > 0 and unicodedata.normalize("NFKD", s[i - 1])[0].lower() in "aeiou":
            continue
        out.append(ch)
    return "".join(out)


def repair(stem: str) -> str:
    """Undo encoding damage in a filename stem. Idempotent; safe on a clean name."""
    s = stem

    # 1. URL-encoded — only when it really looks encoded, so a name that legitimately
    #    contains '+' (rare, but "C++ notes" exists) is not rewritten.
    if s.count("+") >= 2 and " " not in s:
        s = unquote_plus(s)

    # 2. Compatibility forms: full-width colon and friends fold to ASCII.
    s = unicodedata.normalize("NFKC", s)
    s = s.replace(":", "-")

    # 3./4. Mojibake, each guarded on accent count.
    s = _try_recode(s, "cp1252", "utf-8")
    s = _try_recode(s, "cp1251", "latin-1")

    # 5. Detached combining marks.
    s = _fix_detached_marks(s)

    # 6. A trailing unclosed-paren fragment is never meaningful.
    s = _UNCLOSED_PAREN.sub("", s)

    return s.strip()


def slug(text: str, *, max_len: int = MAX_STEM) -> str:
    """ASCII-fold to lowercase kebab-case. Empty input yields an empty string.

    Cyrillic is transliterated rather than dropped. Folding to ASCII and discarding
    what will not fit turns a wholly non-Latin name into an empty string, and the
    callers then fall back to a hash — so ``Донаты.xlsx`` (a donor sheet holding names,
    phone numbers and tax ids) filed itself as ``031dda10.xlsx`` and became impossible
    to identify without opening it. For an operator whose documents are largely Russian
    that is not an edge case.
    """
    if not text:
        return ""
    text = _transliterate(text)
    folded = unicodedata.normalize("NFKD", text)
    ascii_only = "".join(c for c in folded if not unicodedata.combining(c))
    ascii_only = ascii_only.encode("ascii", "ignore").decode("ascii")
    out = _MULTI_DASH.sub("-", _NON_SLUG.sub("-", ascii_only.lower())).strip("-")
    return out[:max_len].rstrip("-")


def normalized_stem(stem: str) -> str:
    """The key near-duplicate grouping uses: repaired, slugged, dup markers removed.

    ``INV-000022-25 (1)`` and ``INV-000022-25`` collapse to the same key.
    ``Promesse d'embauche Sergei`` and ``Promesse d'embauche SergeiM`` do not — which
    is correct, they are different documents.
    """
    s = repair(stem)
    s = _DUP_MARKER.sub("", s)
    low = s.lower()
    for token in _NEAR_DUP_TOKENS:
        if low.endswith(token):
            s = s[: -len(token)]
            low = s.lower()
    return slug(s)
