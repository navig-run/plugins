"""
Splitting the 2013 Google+ follow list into interests, organisations and people.

The archive tier is everything with no phone, no email and no handle — roughly
9,500 entries, almost all of them a name plus a dead ``google.com/profiles/``
URL.  None of it enters the database.  What it still holds is a record of what
the operator followed in 2013, and that is worth keeping as a list of
*interests* even though it is worthless as contact data.

The classifier leans on a structural signal verified in this archive rather
than on guesswork about names: **Google+ filled the vCard ``N:`` surname field
for people and left it empty for pages.**  Of the 7,799 follow-list cards, the
5,988 with no surname collapse to ~31 distinct names — `Android`, `Audacity`,
`FreeBSD`, `NASA`, `PHP`, `Лайфхакер` — while the 1,811 with a surname are real
humans.

It is a heuristic and it is wrong at the edges, so it does not pretend
otherwise: confident calls go to the interests and organisations lists, genuine
coin-flips (`Mark`, `Scott`, `Chronopost`) go to a short *review* list, and
every row carries the reason it landed where it did.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

#: Words that mark a company, shop or public service — an organisation you
#: might phone, not a topic you follow.
_ORG_WORDS = {
    "inc", "ltd", "llc", "sarl", "sas", "gmbh", "corp", "company",
    "agence", "agency", "annuaire", "bureau",
    "shop", "store", "market", "boutique", "cafe", "restaurant", "hotel",
    "school", "university", "institute", "foundation", "association",
    "support", "service", "services", "assurance", "banque", "bank",
    "mutuel", "emploi", "poste", "pharmacie", "clinique", "hopital",
    "сервис", "компания", "магазин", "банк", "аптека", "поликлиника",
}

#: Words that mark media, a product, a channel or a scene — an interest.
_MEDIA_WORDS = {
    "tv", "radio", "news", "press", "magazine", "blog", "podcast", "channel",
    "game", "games", "gaming", "music", "records", "band", "show", "film",
    "media", "team", "club", "group", "project", "lab", "labs", "crew",
    "software", "systems", "solutions", "tech", "technologies", "studio",
    "studios", "forum", "wiki", "community",
    "новости", "студия", "музыка", "игры",
}

#: Standalone names that are unambiguously products, platforms or media.
#: Taken from the actual distinct values in this archive, not invented.
_KNOWN_ENTITIES = {
    "android", "audacity", "battlefield", "codrops", "earth", "freebsd",
    "freemute", "gmail", "google", "google+", "googleplus", "homeland",
    "machinima", "mashable", "nasa", "php", "linux", "ubuntu", "firefox",
    "chrome", "youtube", "twitter", "facebook", "instagram", "anonymous",
    "wikipedia", "github", "stackoverflow", "reddit", "spotify", "netflix",
    "minecraft", "steam", "arduino", "raspberry pi",
    "cyanogenmod", "dokuwiki", "nginx", "nginxtips", "phpmyadmin",
    "playstation", "xbox", "ubisoft", "rockstar games", "machinima",
    "korben", "meganewsrussia", "twitsay", "codrops", "mashable",
    "лайфхакер", "хабрахабр", "хабр",
}

#: Named companies and public services the archive actually contains.  These
#: are places you phone, not topics you follow, and several of them are why
#: the archive holds French short codes ('3949', '36102') that are not
#: E.164 numbers and never became contacts.
_KNOWN_ORGS = {
    "chronopost", "monoprix", "orange", "orange 2", "samu", "repondeur",
    "pole emploi", "credit mutuel", "annuaire", "la poste", "sncf",
    "free", "sfr", "bouygues", "edf", "caf", "ameli", "urssaf",
    "calendar", "incomm",
}

#: Leading article that marks a title rather than a person.
_TITLE_PREFIX = re.compile(r"^(the|le|la|les|el|los)\s+", re.I)

#: A lowercase account handle: 'barkxer', 'aketenlibre', 'arnaudfallet'.
_LOWER_HANDLE = re.compile(r"^[a-zа-яё0-9][a-zа-яё0-9._\-]*$")

#: Skype, Facebook, MySpace and ICQ write their internal account id into the
#: display-name slot: 'live:pr_r_12', 'facebook:bouuuh', 'myspace:roland8725'.
#: 57 of the archive's "unclear" entries were nothing but these.
_ACCOUNT_ID = re.compile(r"^(live|facebook|fb|skype|msn|vk|myspace|icq|aim):",
                         re.I)

#: Symbols people wrap screen names in: ♔ ✖ † ☜ ♡ ☞ ★ ✞ ღ ♛ ♠ and emoji.
_DECORATED = re.compile(
    r"[←-⯿☀-➿️　-〿"
    r"🀀-🫿]"
)

#: An email address or a URL pasted into the name field.
_EMAIL_NAME = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_URL_NAME = re.compile(r"^(https?://|www\.)", re.I)

#: A phone number that ended up in the name field.
_NUMERIC_NAME = re.compile(r"^[+\d][\d\s().+-]{5,}$")

INTEREST = "interest"
ORG = "org"
PERSON = "person"
HANDLE = "handle"
UNCLEAR = "unclear"
EMPTY = "empty"


@dataclass
class Verdict:
    kind: str
    reason: str


def _words(name: str) -> list[str]:
    """
    Lowercase word tokens, with accents folded away.

    The combining marks NFKD produces must be *dropped*, not left in the
    string: a combining breve is not a word character, so leaving it turned
    'Лайфхакер' into ['лаи', 'фхакер'] and 'Sète' into ['se', 'te'], and no
    accented or Cyrillic name ever matched the known-entity lists.
    """
    decomposed = unicodedata.normalize("NFKD", name).lower()
    folded = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.findall(r"[^\W_]+", folded, flags=re.UNICODE)


def _fold(text: str) -> str:
    """The comparison key: both the input and the word lists go through it."""
    return " ".join(_words(text))


def dedup_key(name: str) -> str:
    """
    Fold a name for de-duplication, so '📞Сервис 888' and 'Сервис 888' are one
    entry rather than two.  Emoji and punctuation carry no meaning here.
    """
    return " ".join(_words(name))


#: The word lists are folded through _fold() at import so both sides of every
#: comparison agree: "Лайфхакер" folds to "лаифхакер" (й -> и), and a literal
#: set membership test against the unfolded spelling would never match.
_ORG_WORDS = {_fold(w) for w in _ORG_WORDS}
_MEDIA_WORDS = {_fold(w) for w in _MEDIA_WORDS}
_KNOWN_ENTITIES = {_fold(w) for w in _KNOWN_ENTITIES}
_KNOWN_ORGS = {_fold(w) for w in _KNOWN_ORGS}


def _has_internal_capital(name: str) -> bool:
    """True for FreeBSD / ChannelProXima / NASA — brand-shaped capitalisation."""
    letters = [ch for ch in name if ch.isalpha()]
    if len(letters) < 2:
        return False
    if all(ch.isupper() for ch in letters):
        return True
    return any(ch.isupper() for ch in letters[1:])


def classify(name: str, surname: str = "",
             given_names: frozenset[str] = frozenset()) -> Verdict:
    """
    Decide what an archive-tier entry is.

    Order matters: an explicit entity signal beats the structural surname rule,
    because Google+ did fill in a "surname" for titles like *The Big Bang
    Theory* (``N:Theory;Big;Bang;The;``).
    """
    clean = (name or "").strip()
    if not clean:
        return Verdict(EMPTY, "card carries no name at all")

    tokens = _words(clean)
    if not tokens:
        return Verdict(EMPTY, "name is only symbols")

    if _ACCOUNT_ID.match(clean):
        return Verdict(HANDLE, "an account id, not a name")
    if _NUMERIC_NAME.match(clean):
        return Verdict(HANDLE, "a phone number in the name field")
    if _EMAIL_NAME.match(clean):
        return Verdict(HANDLE, "an email address in the name field")
    if _URL_NAME.match(clean):
        return Verdict(HANDLE, "a URL in the name field")
    # A name wrapped in decorative symbols is a messenger or gaming tag —
    # '♔LegkOFFa♔', '✖†ISTERICHKA†✖'.  A person is behind it, but it is a
    # screen name, not something to file as an interest.  Checked before the
    # surname rules, because what N: happens to contain does not change that.
    if _DECORATED.search(clean) and any(ch.isalpha() for ch in clean):
        return Verdict(HANDLE, "a decorated screen name, not a plain name")

    lowered = " ".join(tokens)
    if lowered in _KNOWN_ENTITIES:
        return Verdict(INTEREST, "known product/platform")
    if lowered in _KNOWN_ORGS:
        return Verdict(ORG, "known company or public service")

    org_hits = sorted(_ORG_WORDS.intersection(tokens))
    if org_hits:
        return Verdict(ORG, f"organisation word: {', '.join(org_hits)}")

    media_hits = sorted(_MEDIA_WORDS.intersection(tokens))
    if media_hits:
        return Verdict(INTEREST, f"media/product word: {', '.join(media_hits)}")

    if _TITLE_PREFIX.match(clean):
        return Verdict(INTEREST, "title-style leading article")

    # A bare lowercase token is an account handle however the N field was
    # filled in: Skype wrote the handle into the surname slot too
    # (`N:alena.klimenkova3;;;;`), so a surname here proves nothing.
    if _LOWER_HANDLE.match(clean):
        return Verdict(HANDLE, "all-lowercase account handle, not a display name")

    # Skype wrote the display name into the surname slot as well
    # (`N:ChannelProXima;;;;`).  A surname that merely repeats the name is
    # not evidence of a person, so ignore it and fall through to the
    # structural rules below.
    surname_is_informative = bool(surname.strip()) and _fold(surname) != lowered

    if surname_is_informative:
        if len(tokens) >= 2:
            return Verdict(PERSON, "first + last name")
        return Verdict(UNCLEAR, "single token with a surname field")

    # No surname — the Google+ signal that this was a page, not a person.
    if len(tokens) == 1:
        if _has_internal_capital(clean):
            return Verdict(INTEREST, "brand-shaped capitalisation, no surname")
        # A lone first name is only evidence of a person if the archive uses
        # it as one elsewhere.  `given_names` is built from the cards that DO
        # carry a surname, so "Olga" and "Дарья" are recognised from this
        # operator's own contacts rather than from a guessed name dictionary.
        if tokens[0] in given_names:
            return Verdict(PERSON, "a given name used elsewhere in the archive")
        return Verdict(UNCLEAR, "single capitalised token, no surname")
    return Verdict(UNCLEAR, "no surname, multi-token name")


@dataclass
class Entry:
    """One archive-tier name, aggregated across every card that carried it."""

    name: str
    kind: str
    reason: str
    cards: int = 0
    urls: list[str] = field(default_factory=list)
    source_files: set[str] = field(default_factory=set)


@dataclass
class ArchiveSplit:
    interests: list[Entry] = field(default_factory=list)
    organisations: list[Entry] = field(default_factory=list)
    unclear: list[Entry] = field(default_factory=list)
    people: list[Entry] = field(default_factory=list)
    handles: list[Entry] = field(default_factory=list)
    empty_cards: int = 0
    empty_clusters: int = 0

    @property
    def dropped_total(self) -> int:
        return sum(
            len(bucket) for bucket in (self.people, self.handles, self.unclear)
        ) + self.empty_clusters


def collect_given_names(cards) -> frozenset[str]:
    """
    First-name tokens taken from every card that also carries a surname.

    Evidence from the operator's own archive beats a bundled name list: it
    covers the Russian, French and Belarusian names actually present here,
    and it cannot go stale.
    """
    out: set[str] = set()
    for card in cards:
        if not card.surname.strip() or not card.given.strip():
            continue
        for token in _words(card.given):
            if len(token) >= 3:
                out.add(token)
    return frozenset(out)


def split_archive(clusters, given_names: frozenset[str] = frozenset()) -> ArchiveSplit:
    """
    Sort archive-tier clusters into interests, organisations, people and junk.

    Nothing here enters the database.  Every bucket is returned in full so the
    report can account for all of it: the operator asked for the non-people to
    become a list of interests and the rest to be dropped, and "dropped" should
    still be inspectable.
    """
    split = ArchiveSplit()
    buckets: dict[str, dict[str, Entry]] = {
        INTEREST: {}, ORG: {}, UNCLEAR: {}, PERSON: {}, HANDLE: {},
    }

    for cluster in clusters:
        name = cluster.display_name().strip()
        surname = ""
        for card in cluster.cards:
            if card.surname.strip():
                surname = card.surname.strip()
                break

        verdict = classify(name, surname, given_names)

        if verdict.kind == EMPTY:
            split.empty_clusters += 1
            split.empty_cards += len(cluster.cards)
            continue

        key = dedup_key(name)
        bucket = buckets[verdict.kind]
        entry = bucket.get(key)
        if entry is None:
            entry = Entry(name=name, kind=verdict.kind, reason=verdict.reason)
            bucket[key] = entry
        entry.cards += len(cluster.cards)
        entry.source_files.update(cluster.source_files())
        for url in cluster.urls():
            if url not in entry.urls and len(entry.urls) < 3:
                entry.urls.append(url)

    def ordered(bucket: dict[str, Entry]) -> list[Entry]:
        return sorted(bucket.values(), key=lambda e: (-e.cards, e.name.lower()))

    split.interests = ordered(buckets[INTEREST])
    split.organisations = ordered(buckets[ORG])
    split.unclear = ordered(buckets[UNCLEAR])
    split.people = ordered(buckets[PERSON])
    split.handles = ordered(buckets[HANDLE])
    return split
