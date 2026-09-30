"""Read a document's expiry date out of its own text, so reminders work without typing.

Two sources, both chosen for precision over recall — a wrong expiry date produces a
reminder about nothing, which trains the operator to ignore reminders:

1. **The machine-readable zone** (MRZ) printed on passports (TD3) and ID cards / residence
   permits (TD1). Its expiry field carries an ICAO 9303 check digit, so an OCR misread is
   *detected and rejected* rather than trusted.
2. **A labelled date**: "date d'expiration : 14/03/2031", "date of expiry 12 MAR 2029",
   "valable jusqu'au …", "gültig bis …". A bare date is never taken — a date of birth or
   of issue sits right next to the expiry on every ID.

Anything implausible (before 2000, more than 30 years out) is dropped, and so is a
labelled date already more than ``STALE_LABEL_DAYS`` past — that is a date the
document QUOTES (a permit's expiry printed on a receipt), not its own. The caller marks
the result as *detected*, so it is shown as read-from-the-document, never as typed.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date

_MONTHS = {
    # English
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    # French (accents folded)
    "janv": 1, "janvier": 1, "fevr": 2, "fev": 2, "fevrier": 2, "mars": 3, "avr": 4,
    "avril": 4, "mai": 5, "juin": 6, "juil": 7, "juillet": 7, "aout": 8, "septembre": 9,
    "octobre": 10, "novembre": 11, "decembre": 12,
    # German / Spanish / Italian — the common short forms
    "maerz": 3, "marz": 3, "okt": 10, "dez": 12, "ene": 1, "abr": 4, "ago": 8, "dic": 12,
    "gen": 1, "mag": 5, "giu": 6, "lug": 7, "set": 9, "ott": 10,
}

_LABEL = re.compile(
    r"date\s*d\W?\s*expiration|date\s*de\s*fin\s*de\s*validite|expire\s*le|"
    r"valable\s*jusqu\W?\s*au|valide\s*jusqu\W?\s*au|fin\s*de\s*validite|"
    r"date\s*of\s*expiry|expiry\s*date|expiration\s*date|date\s*of\s*expiration|"
    r"expires?(?:\s*on)?|valid\s*(?:until|thru|through)|exp\.?\s*date|"
    r"gultig\s*bis|ablaufdatum|fecha\s*de\s*caducidad|valido\s*hasta|scadenza|"
    r"data\s*di\s*scadenza",
)

_NUMERIC = re.compile(r"\b(\d{1,2})\s*[./\- ]\s*(\d{1,2})\s*[./\- ]\s*(\d{4}|\d{2})\b")
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_WORDY = re.compile(r"\b(\d{1,2})\s*[ ./\-]?\s*([a-z]{3,9})\.?\s*[ ./\-]?\s*(\d{4}|\d{2})\b")


@dataclass(frozen=True)
class Found:
    expires: str          # YYYY-MM-DD
    source: str           # "mrz" | "label"
    evidence: str         # the words the date was read from


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def _year(y: str) -> int:
    n = int(y)
    return 2000 + n if len(y) == 2 else n


def _plausible(d: date, today: date) -> bool:
    return 2000 <= d.year and d.year <= today.year + 30


#: A LABELLED date this far in the past when it is read is a quote, not the
#: document's own expiry. Measured 2026-09-27: a France Travail registration receipt
#: filed that day carried "date fin de validité du titre : 12/07/2025" — the expiry
#: of the residence permit it mentions — and became "a financial document expired 443
#: day(s) ago", renewing nothing. Nobody files a paper to be told it lapsed a year
#: ago; a recently lapsed one (an insurance certificate from last month) still counts.
#: The machine-readable zone is exempt: its check-digit-verified expiry IS the
#: document's own, and an expired passport is a real renewal.
STALE_LABEL_DAYS = 90


def _make(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _first_date(fragment: str) -> tuple[date, str] | None:
    """The first date in *fragment* (already folded), as written in Europe (D/M/Y)."""
    best: tuple[int, date, str] | None = None
    for m in _ISO.finditer(fragment):
        d = _make(int(m[1]), int(m[2]), int(m[3]))
        if d and (best is None or m.start() < best[0]):
            best = (m.start(), d, m[0])
    for m in _NUMERIC.finditer(fragment):
        d = _make(_year(m[3]), int(m[2]), int(m[1]))
        if d and (best is None or m.start() < best[0]):
            best = (m.start(), d, m[0])
    for m in _WORDY.finditer(fragment):
        month = _MONTHS.get(m[2])
        d = _make(_year(m[3]), month, int(m[1])) if month else None
        if d and (best is None or m.start() < best[0]):
            best = (m.start(), d, m[0])
    return (best[1], best[2]) if best else None


# ── MRZ (ICAO 9303) ─────────────────────────────────────────────────────────


def _check_digit(field: str) -> int:
    total = 0
    for i, ch in enumerate(field):
        if ch.isdigit():
            v = int(ch)
        elif "A" <= ch <= "Z":
            v = ord(ch) - 55
        else:  # '<'
            v = 0
        total += v * (7, 3, 1)[i % 3]
    return total % 10


def _mrz_expiry(line: str) -> str | None:
    """Expiry from a TD3 (passport, 44) or TD1 (ID card, 30) line 2, check digit verified."""
    candidates = []
    if len(line) >= 28:   # TD3 line 2: expiry at 21:27, check at 27
        candidates.append((line[21:27], line[27]))
    if len(line) >= 15:   # TD1 line 2: expiry at 8:14, check at 14
        candidates.append((line[8:14], line[14]))
    for field, check in candidates:
        if field.isdigit() and check.isdigit() and _check_digit(field) == int(check):
            d = _make(2000 + int(field[:2]), int(field[2:4]), int(field[4:6]))
            if d:
                return d.isoformat()
    return None


def _mrz_lines(text: str) -> list[str]:
    out = []
    for raw in text.splitlines():
        s = raw.strip().replace(" ", "").upper()
        if 28 <= len(s) <= 46 and re.fullmatch(r"[A-Z0-9<]+", s) and "<" in s:
            out.append(s)
    return out


# ── public ──────────────────────────────────────────────────────────────────


def find_expiry(text: str, *, today: date | None = None) -> Found | None:
    """The document's expiry date, or None when it cannot be read with confidence."""
    if not text:
        return None
    today = today or date.today()
    for line in _mrz_lines(text):
        iso = _mrz_expiry(line)
        if iso and _plausible(date.fromisoformat(iso), today):
            return Found(iso, "mrz", line)
    folded = _fold(text)
    for m in _LABEL.finditer(folded):
        window = folded[m.end(): m.end() + 40]
        hit = _first_date(window)
        if hit and _plausible(hit[0], today) and (today - hit[0]).days <= STALE_LABEL_DAYS:
            evidence = " ".join((folded[m.start(): m.end()] + window[: window.find(hit[1]) + len(hit[1])]).split())
            return Found(hit[0].isoformat(), "label", evidence)
    return None
