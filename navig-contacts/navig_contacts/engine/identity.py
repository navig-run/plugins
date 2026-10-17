"""
Identifier normalisation — the gate that decides what counts as reachable.

This module is deliberately dependency-light and imports nothing from the rest
of the package (``db`` imports ``name_key`` from here, so the arrow must only
point one way).

The rule the whole import hangs on: **a contact is legit only if it has a phone
number**, and "has a phone number" means libphonenumber agrees the number is
valid for some region — not that the string contained digits.  The archive is
full of strings that look like numbers and are not: the short codes ``33700``
and ``36102``, the placeholder ``+7**********``, the caller-ID-block prefix
``#31#15``, and doubled country codes like ``+7+79603166750``.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable, Optional

import phonenumbers

#: The schema owns which identifiers may merge two records; re-exported
#: here so the engine and the unique index cannot disagree.
from navig_contacts.store import HARD_KINDS  # noqa: F401

# ---------------------------------------------------------------------------
# Regions
# ---------------------------------------------------------------------------

#: Fallback region for a bare national number with no other evidence.
#: France, because it is what the archive overwhelmingly is: 2,236 of the ~3,000
#: international numbers are +33, and the 10-digit `0X XX XX XX XX` block is the
#: French national format.  Every number that lands here is reported with the
#: region that was assumed, so the guess is auditable rather than silent.
DEFAULT_REGION = "FR"

#: vCard ADR country fields and X-SKYPE-COUNTRY codes seen in this archive.
#: Both the English and Russian spellings occur, sometimes on the same person.
COUNTRY_TO_REGION = {
    "france": "FR", "франция": "FR", "fr": "FR",
    "russia": "RU", "россия": "RU", "russian federation": "RU", "ru": "RU",
    "belarus": "BY", "беларусь": "BY", "белоруссия": "BY", "by": "BY",
    "ukraine": "UA", "украина": "UA", "ua": "UA",
    "germany": "DE", "германия": "DE", "de": "DE",
    "switzerland": "CH", "ch": "CH",
    "belgium": "BE", "be": "BE",
    "united kingdom": "GB", "uk": "GB", "gb": "GB",
    "united states": "US", "usa": "US", "us": "US",
    "spain": "ES", "es": "ES",
    "italy": "IT", "it": "IT",
    "turkey": "TR", "tr": "TR",
    "netherlands": "NL", "nl": "NL",
    "poland": "PL", "pl": "PL",
    "portugal": "PT", "pt": "PT",
    "canada": "CA", "ca": "CA",
    "morocco": "MA", "ma": "MA",
    "tunisia": "TN", "tn": "TN",
    "algeria": "DZ", "dz": "DZ",
    "kazakhstan": "KZ", "kz": "KZ",
    "moldova": "MD", "md": "MD",
    "latvia": "LV", "lv": "LV",
    "lithuania": "LT", "lt": "LT",
    "estonia": "EE", "ee": "EE",
    "japan": "JP", "jp": "JP",
    "china": "CN", "cn": "CN",
    "australia": "AU", "au": "AU",
    "romania": "RO", "ro": "RO",
    "bulgaria": "BG", "bg": "BG",
    "greece": "GR", "gr": "GR",
    "israel": "IL", "il": "IL",
    "brazil": "BR", "br": "BR",
    "india": "IN", "in": "IN",
    "sweden": "SE", "se": "SE",
    "norway": "NO", "no": "NO",
    "finland": "FI", "fi": "FI",
    "denmark": "DK", "dk": "DK",
    "austria": "AT", "at": "AT",
    "czech republic": "CZ", "cz": "CZ",
    "hungary": "HU", "hu": "HU",
    "serbia": "RS", "rs": "RS",
    "croatia": "HR", "hr": "HR",
    "ireland": "IE", "ie": "IE",
    "mexico": "MX", "mx": "MX",
    "argentina": "AR", "ar": "AR",
}


def region_for(country: Optional[str]) -> Optional[str]:
    """Map a vCard ADR country / X-SKYPE-COUNTRY value to an ISO region code."""
    if not country:
        return None
    key = country.strip().lower()
    if key in COUNTRY_TO_REGION:
        return COUNTRY_TO_REGION[key]
    # "Languedoc-Roussillon;;France" style leftovers
    for part in re.split(r"[;,/]", key):
        part = part.strip()
        if part in COUNTRY_TO_REGION:
            return COUNTRY_TO_REGION[part]
    return None


# ---------------------------------------------------------------------------
# Phones
# ---------------------------------------------------------------------------

#: Doubled country code: "+7+79603166750", "+375+375333934199".  Skype wrote
#: these when a user already had a "+"-prefixed number and it prefixed again.
_DOUBLED_CC = re.compile(r"^\+(\d{1,4})\+(\d.*)$")
#: French "hide my caller ID" dial prefix, not part of the number.
_CALLER_ID_BLOCK = re.compile(r"^#\d{2}#")
_KEEPABLE = re.compile(r"[^0-9+]")


def repair_phone(raw: str) -> str:
    """
    Undo the specific manglings this archive contains, before parsing.

    Returns a cleaned string; it does *not* decide validity — that is
    libphonenumber's job in :func:`normalize_phone`.
    """
    s = (raw or "").strip()
    if not s:
        return ""
    s = _CALLER_ID_BLOCK.sub("", s)
    m = _DOUBLED_CC.match(s)
    if m:
        cc, rest = m.group(1), m.group(2)
        # "+7+79603166750" -> the rest already carries the country code
        s = "+" + rest if rest.startswith(cc) else "+" + cc + rest
    if s.count("+") > 1:
        # any remaining multi-"+" mess: keep from the last "+" onwards
        s = "+" + s.rsplit("+", 1)[1]
    s = _KEEPABLE.sub("", s)
    if s.startswith("00"):
        s = "+" + s[2:]
    # a "+" anywhere but the front is noise
    if "+" in s[1:]:
        s = s[0] + s[1:].replace("+", "")
    return s


def normalize_phone(
    raw: str,
    region_hints: Iterable[Optional[str]] = (),
) -> tuple[Optional[str], str]:
    """
    Validate one TEL value and return ``(e164_or_None, reason)``.

    ``region_hints`` are tried in order for numbers with no ``+``; the first
    region that yields a *valid* number wins, and :data:`DEFAULT_REGION` is the
    last resort.  ``reason`` explains a rejection, or names the region that was
    assumed, so every judgement call lands in the rejected/assumed report
    instead of disappearing.
    """
    cleaned = repair_phone(raw)
    if not cleaned:
        return None, "no digits"

    digits = cleaned.lstrip("+")
    if len(digits) < 7:
        return None, f"too short ({len(digits)} digits) — short code or fragment"
    if len(digits) > 15:
        return None, f"too long ({len(digits)} digits) — E.164 allows 15"

    candidates: list[Optional[str]] = [None] if cleaned.startswith("+") else []
    if not cleaned.startswith("+"):
        seen: set[str] = set()
        for hint in list(region_hints) + [DEFAULT_REGION]:
            if hint and hint not in seen:
                seen.add(hint)
                candidates.append(hint)

    last_err = "unparseable"
    for region in candidates:
        try:
            parsed = phonenumbers.parse(cleaned, region)
        except phonenumbers.NumberParseException as exc:
            last_err = str(exc.args[0]) if exc.args else "parse error"
            continue
        if phonenumbers.is_valid_number(parsed):
            e164 = phonenumbers.format_number(
                parsed, phonenumbers.PhoneNumberFormat.E164
            )
            why = "explicit +" if region is None else f"assumed region {region}"
            return e164, why
        last_err = f"not a valid number{'' if region is None else f' for {region}'}"

    # Last resort: a bare number that failed every region may already carry its
    # own country code and just be missing the "+", e.g. '79370971896' (a valid
    # RU mobile) on a card with no address to hint from.  Only reached once
    # every region has refused it, and libphonenumber still has to agree.
    if not cleaned.startswith("+"):
        try:
            parsed = phonenumbers.parse("+" + digits, None)
            if phonenumbers.is_valid_number(parsed):
                return (
                    phonenumbers.format_number(
                        parsed, phonenumbers.PhoneNumberFormat.E164
                    ),
                    "read as an international number missing its +",
                )
        except phonenumbers.NumberParseException:
            pass

    return None, last_err


# ---------------------------------------------------------------------------
# Emails
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[A-Za-z]{2,}$")


def normalize_email(raw: str) -> Optional[str]:
    """Lowercase and syntax-check one EMAIL value; None if it is not one."""
    s = (raw or "").strip().strip("<>").rstrip(".,;").lower()
    if not s or not _EMAIL_RE.match(s):
        return None
    return s


# ---------------------------------------------------------------------------
# Handles and profile URLs
# ---------------------------------------------------------------------------

_HANDLE_OK = re.compile(r"^[A-Za-z0-9._\-]{2,64}$")

_URL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?:https?://)?(?:www\.)?t\.me/([A-Za-z0-9_]{3,32})", re.I), "telegram"),
    (re.compile(r"^fb://p(?:ro|eo)file/(.+)$", re.I), "facebook"),
    (re.compile(r"(?:https?://)?(?:www\.)?facebook\.com/(?:profile\.php\?id=)?"
                r"([A-Za-z0-9._\-]{3,})", re.I), "facebook"),
    (re.compile(r"(?:https?://)?(?:www\.)?vk(?:ontakte)?\.(?:com|ru)/"
                r"([A-Za-z0-9._\-]{2,})", re.I), "vk"),
]


def normalize_handle(raw: str) -> Optional[str]:
    """Lowercase a bare account handle; None if it is not handle-shaped."""
    s = (raw or "").strip().lstrip("@")
    if not s or not _HANDLE_OK.match(s):
        return None
    return s.lower()


def parse_url_identifier(raw: str) -> Optional[tuple[str, str]]:
    """
    Classify a URL into ``(kind, value_norm)``.

    Google+ profile URLs are returned as ``url`` rather than a typed identity:
    they are unique per person, but they belong to the 2013 follow list that
    never enters the database, and giving them merge power would be pointless
    risk.
    """
    s = (raw or "").strip().replace("\\", "")
    if not s:
        return None
    for pattern, kind in _URL_PATTERNS:
        m = pattern.search(s)
        if m:
            value = m.group(1).strip().lower()
            if value and value not in ("profile.php", "home", "pages"):
                return kind, value
    return None


def is_google_profile(raw: str) -> bool:
    """True for the google.com/profiles/... URLs of the 2013 follow list."""
    return "google.com/profiles" in (raw or "").replace("\\", "").lower()


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

def _strip_marks(text: str) -> str:
    """NFKD-fold and drop combining marks, so 'Sète' and 'Sete' agree."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def name_key(name: Optional[str]) -> str:
    """
    A comparable form of a display name, for search and review only.

    Tokens are sorted, so 'Adnot Florian' and 'Florian Adnot' collapse to the
    same key.  Emoji, punctuation and the leading '!' the archive uses as a
    sort hack are dropped.

    **This is never a merge key.**  Measured on this data, bare-name matching
    fuses unrelated people: 74 names are shared between the vCard archive and
    the existing database and every one of them is a generic first name
    ('anna', 'alex', 'diana').  Merging happens on phone/email/handle only.
    """
    if not name:
        return ""
    folded = _strip_marks(str(name)).lower()
    kept = [
        ch if (unicodedata.category(ch)[0] in ("L", "N")) else " "
        for ch in folded
    ]
    tokens = "".join(kept).split()
    return " ".join(sorted(tokens))


#: Skype and Facebook write their internal account id into the display-name
#: field: 'live:pr_r_12', 'facebook:vova.djachkovskijj'.  These are account
#: ids wearing a name-shaped slot, and treating them as names put two of
#: them on contacts and made four merges look suspicious that were not.
_ACCOUNT_ID_PREFIX = re.compile(r"^(live|facebook|fb|skype|msn|vk):", re.I)


def looks_like_handle(name: Optional[str]) -> bool:
    """
    True when a display name is really an account handle ('vincent.olivan',
    'totara04', 'alena.klimenkova3', 'live:pr_r_12') rather than a human name.

    Used to prefer a real name over a handle when choosing which of a merged
    cluster's names becomes the contact's display name, and to keep account
    ids out of the "two unrelated people" merge check.
    """
    s = (name or "").strip()
    if not s:
        return False
    if _ACCOUNT_ID_PREFIX.match(s):
        return True
    if " " in s:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9._\-]+", s)) and bool(
        re.search(r"[._\-]|\d", s)
    )


def display_name_score(name: Optional[str]) -> int:
    """
    Rank a candidate display name; higher wins when a cluster is collapsed.

    Prefers a real first+last name, then any human-readable name, then a
    handle, and puts the archive's '!'-prefixed sort hacks and emoji-laden
    variants last.
    """
    s = (name or "").strip()
    if not s:
        return -1
    if _ACCOUNT_ID_PREFIX.match(s):
        return 0  # an account id is the worst possible display name
    score = 0
    tokens = s.split()
    if len(tokens) >= 2:
        score += 4
    if not looks_like_handle(s):
        score += 3
    if not s.startswith("!"):
        score += 2
    if not any(unicodedata.category(ch)[0] in ("S", "C") for ch in s):
        score += 2
    if any(unicodedata.category(ch)[0] == "L" for ch in s):
        score += 1
    return score
