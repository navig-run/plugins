"""
Record linkage — turning 19,013 cards into the people behind them.

Two rules govern everything here:

1. **Merging happens on hard identifiers only** — phone, email, skype,
   telegram, facebook, vk.  Names are never a merge key.  Measured on this
   archive, name matching fuses unrelated people: the only 74 names shared
   between the vCard files and the existing database are generic first names
   ('anna', 'alex', 'diana').  Name similarity is reported as a *suggestion*
   for a human, and never applied.

2. **A cluster is only a contact if it has a validated phone.**  Everything
   else is a lead, filed at a weaker tier so it stays searchable without
   pretending to be someone you can call.

Merges are logged row-by-row in ``merge_log`` with a JSON payload, so any of
them can be undone later without going back to the source files.
"""
from __future__ import annotations

import collections
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional

from navig_contacts.store import (  # the schema owns these definitions
    HARD_KINDS,
    IDENTIFIER_ROUTE_NETWORK,
)

from .identity import (
    display_name_score,
    looks_like_handle,
    is_google_profile,
    name_key,
    normalize_email,
    normalize_handle,
    normalize_phone,
    parse_url_identifier,
    region_for,
)
from .models import ImportSummary
from .vcard import VCard

#: uid prefix per identifier kind.  Prefixes cannot collide with the existing
#: Telegram uids, which are bare usernames with no prefix.
_UID_PREFIX = {
    "phone": "p", "email": "m", "skype": "s",
    "telegram": "t", "facebook": "f", "vk": "v",
}

_BDAY_RE = re.compile(r"^(\d{4})-?(\d{2})-?(\d{2})")


def _utcnow() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def normalize_bday(raw: Optional[str]) -> Optional[str]:
    """`19880527` and `1988-05-27` both become `1988-05-27`; junk becomes None."""
    if not raw:
        return None
    m = _BDAY_RE.match(raw.strip())
    if not m:
        return None
    year, month, day = m.groups()
    if not ("1900" <= year <= "2030") or not ("01" <= month <= "12"):
        return None
    return f"{year}-{month}-{day}"


@dataclass
class RejectedPhone:
    raw: str
    reason: str
    source_file: str
    card_index: int
    name: str


@dataclass
class AssumedPhone:
    """A number that only validated because a region was inferred for it.

    Recorded so every guess the importer made is listed in the report rather
    than buried: a bare '0613601584' becoming +33613601584 is an assumption
    about where the operator was, not a fact from the card.
    """

    e164: str
    raw: str
    reason: str
    source_file: str
    name: str


@dataclass
class CardKeys:
    """The identifiers one card contributes, after normalisation."""

    phones: list[tuple[str, str]] = field(default_factory=list)   # (e164, raw)
    emails: list[tuple[str, str]] = field(default_factory=list)
    handles: list[tuple[str, str, str]] = field(default_factory=list)  # kind,norm,raw
    urls: list[str] = field(default_factory=list)
    rejected: list[RejectedPhone] = field(default_factory=list)
    assumed: list[AssumedPhone] = field(default_factory=list)

    def hard_keys(self) -> list[tuple[str, str]]:
        keys = [("phone", p) for p, _ in self.phones]
        keys += [("email", e) for e, _ in self.emails]
        keys += [(kind, norm) for kind, norm, _ in self.handles]
        return keys


def extract_keys(card: VCard) -> CardKeys:
    """Normalise every identifier on one card, recording what was rejected."""
    out = CardKeys()

    hints = [region_for(card.country), region_for(card.skype_country)]
    hints = [h for h in hints if h]

    seen_phone: set[str] = set()
    for raw in card.tels:
        e164, reason = normalize_phone(raw, hints)
        if e164 is None:
            out.rejected.append(RejectedPhone(
                raw=raw, reason=reason, source_file=card.source_file,
                card_index=card.card_index, name=card.display_name,
            ))
            continue
        if reason != "explicit +":
            out.assumed.append(AssumedPhone(
                e164=e164, raw=raw, reason=reason,
                source_file=card.source_file, name=card.display_name,
            ))
        if e164 not in seen_phone:
            seen_phone.add(e164)
            out.phones.append((e164, raw))

    seen_mail: set[str] = set()
    for raw in card.emails:
        addr = normalize_email(raw)
        if addr and addr not in seen_mail:
            seen_mail.add(addr)
            out.emails.append((addr, raw))

    if card.skype:
        handle = normalize_handle(card.skype)
        if handle:
            out.handles.append(("skype", handle, card.skype))

    seen_handle = {(k, v) for k, v, _ in out.handles}
    for raw in card.urls:
        if is_google_profile(raw):
            out.urls.append(raw)
            continue
        parsed = parse_url_identifier(raw)
        if parsed:
            kind, value = parsed
            if (kind, value) not in seen_handle:
                seen_handle.add((kind, value))
                out.handles.append((kind, value, raw))
        else:
            out.urls.append(raw)

    return out


@dataclass
class Cluster:
    """One person, and every card that turned out to be them."""

    cards: list[VCard] = field(default_factory=list)
    keys: list[CardKeys] = field(default_factory=list)

    # -- identifiers, de-duplicated but order-preserving ------------------
    def phones(self) -> list[tuple[str, str]]:
        return _dedup_pairs(k.phones for k in self.keys)

    def emails(self) -> list[tuple[str, str]]:
        return _dedup_pairs(k.emails for k in self.keys)

    def handles(self) -> list[tuple[str, str, str]]:
        seen: set[tuple[str, str]] = set()
        out: list[tuple[str, str, str]] = []
        for k in self.keys:
            for kind, norm, raw in k.handles:
                if (kind, norm) not in seen:
                    seen.add((kind, norm))
                    out.append((kind, norm, raw))
        return out

    def urls(self) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for k in self.keys:
            for u in k.urls:
                if u not in seen:
                    seen.add(u)
                    out.append(u)
        return out

    # -- derived attributes ----------------------------------------------
    def tier(self) -> str:
        if self.phones():
            return "verified"
        if self.emails():
            return "email"
        if self.handles():
            return "handle"
        return "archive"

    def display_name(self) -> str:
        """
        Best of the cluster's names: a real first+last beats a handle, which
        beats an emoji-laden or '!'-prefixed sort hack.  Ties go to the name
        that appeared on the most cards.
        """
        counts = collections.Counter(
            c.display_name.strip() for c in self.cards if c.display_name.strip()
        )
        if not counts:
            return ""
        return max(counts, key=lambda n: (display_name_score(n), counts[n]))

    def aliases(self) -> list[tuple[str, str, str]]:
        """Every distinct name variant, as (name, name_key, source_file)."""
        seen: set[str] = set()
        out: list[tuple[str, str, str]] = []
        for card in self.cards:
            name = card.display_name.strip()
            if not name:
                continue
            key = name_key(name)
            spelling = name.casefold()
            if spelling in seen:
                continue
            # An emoji-only name ("💋", "(╯︵╰,)") folds to an empty key.
            # It is still the name this person was filed under, so it is
            # kept as an alias; it is simply not searchable by key.
            seen.add(spelling)
            out.append((name, key, card.source_file))
        return out

    def _most_common(self, attr: str) -> Optional[str]:
        vals = [
            getattr(c, attr) for c in self.cards
            if getattr(c, attr) and str(getattr(c, attr)).strip()
        ]
        if not vals:
            return None
        return collections.Counter(str(v).strip() for v in vals).most_common(1)[0][0]

    def city(self) -> Optional[str]:
        return self._most_common("city") or self._most_common("skype_city")

    def country(self) -> Optional[str]:
        return self._most_common("country")

    def org(self) -> Optional[str]:
        return self._most_common("org")

    def title(self) -> Optional[str]:
        return self._most_common("title")

    def language(self) -> Optional[str]:
        return self._most_common("skype_language")

    def birthday(self) -> Optional[str]:
        vals = [normalize_bday(c.bday) for c in self.cards]
        vals = [v for v in vals if v]
        if not vals:
            return None
        return collections.Counter(vals).most_common(1)[0][0]

    def source_files(self) -> list[str]:
        return sorted({c.source_file for c in self.cards})

    def alias(self) -> Optional[str]:
        """
        A deterministic uid, so re-importing the same archive is a no-op
        rather than a second copy of everyone.
        """
        phones = self.phones()
        if phones:
            return f"{_UID_PREFIX['phone']}:{phones[0][0].lstrip('+')}"
        emails = self.emails()
        if emails:
            digest = hashlib.sha1(emails[0][0].encode("utf-8")).hexdigest()[:10]
            return f"{_UID_PREFIX['email']}:{digest}"
        for kind in ("skype", "telegram", "facebook", "vk"):
            for k, norm, _raw in self.handles():
                if k == kind:
                    safe = re.sub(r"[^A-Za-z0-9._\-]", "_", norm)[:48]
                    if safe:
                        return f"{_UID_PREFIX[kind]}:{safe}"
        return None

    def identifier_rows(self) -> list[tuple[str, str, str, int]]:
        """(kind, value_norm, value_raw, is_primary) for every identifier."""
        rows: list[tuple[str, str, str, int]] = []
        for i, (e164, raw) in enumerate(self.phones()):
            rows.append(("phone", e164, raw, 1 if i == 0 else 0))
        for i, (addr, raw) in enumerate(self.emails()):
            rows.append(("email", addr, raw, 1 if i == 0 else 0))
        for kind, norm, raw in self.handles():
            rows.append((kind, norm, raw, 0))
        return rows


def _dedup_pairs(groups: Iterable[list[tuple[str, str]]]) -> list[tuple[str, str]]:
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for group in groups:
        for norm, raw in group:
            if norm not in seen:
                seen.add(norm)
                out.append((norm, raw))
    return out


class _Union:
    """Minimal union-find over card indices."""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


@dataclass
class ClusterResult:
    clusters: list["Cluster"] = field(default_factory=list)
    rejected: list[RejectedPhone] = field(default_factory=list)
    assumed: list[AssumedPhone] = field(default_factory=list)


def build_clusters(cards: list[VCard]) -> ClusterResult:
    """
    Group cards into people on hard identifiers alone.

    Returns the clusters (in first-appearance order) alongside every phone
    value that failed validation and every region that had to be assumed, so
    both are reported rather than silently lost.
    """
    keys = [extract_keys(c) for c in cards]
    rejected = [r for k in keys for r in k.rejected]
    assumed = [a for k in keys for a in k.assumed]

    uf = _Union()
    for i, k in enumerate(keys):
        node = f"c{i}"
        uf.find(node)
        for kind, value in k.hard_keys():
            uf.union(node, f"k:{kind}:{value}")

    grouped: dict[str, Cluster] = {}
    order: list[str] = []
    for i, card in enumerate(cards):
        root = uf.find(f"c{i}")
        if root not in grouped:
            grouped[root] = Cluster()
            order.append(root)
        grouped[root].cards.append(card)
        grouped[root].keys.append(keys[i])

    return ClusterResult(
        clusters=[grouped[r] for r in order],
        rejected=rejected,
        assumed=assumed,
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _existing_ids_for(conn, cluster: Cluster) -> list[int]:
    """
    Which existing contacts already own any of this cluster's hard identifiers.

    Routes are consulted as well as identifiers, and that is not belt-and-braces.
    A contact added by hand (``navig contacts add --phone``) or by the Telegram
    importer has a *route* and no identifier row, so an identifier-only lookup
    does not see it — and importing an address book containing that same number
    would file the person a second time, under a different alias, with the number
    on both. Matching the route attaches instead, and the identifier rows written
    afterwards mean the next import matches directly.
    """
    pairs = [
        (kind, value) for kind, value, _raw, _p in cluster.identifier_rows()
        if kind in HARD_KINDS
    ]
    if not pairs:
        return []
    found: list[int] = []

    def remember(row) -> None:
        if row and row[0] not in found:
            found.append(row[0])

    for kind, value in pairs:
        remember(conn.execute(
            "SELECT contact_id FROM contact_identifiers "
            "WHERE kind = ? AND value_norm = ?", (kind, value),
        ).fetchone())
        network = IDENTIFIER_ROUTE_NETWORK.get(kind)
        if network:
            remember(conn.execute(
                "SELECT contact_id FROM contact_routes "
                "WHERE network = ? COLLATE NOCASE AND address = ?",
                (network, value),
            ).fetchone())
    return found


def currently_flagged(conn) -> list[str]:
    """
    Every contact the suspicious-merge check would print right now.

    Computed from the book rather than the source files, so it works whether
    or not the archive is still on the drive it was imported from.
    """
    def groups(names: list[str]) -> list[set]:
        out: list[set] = []
        for name in names:
            if looks_like_handle(name):
                continue
            tokens = {t for t in name_key(name).split() if len(t) > 2}
            if len(tokens) < 2:
                continue
            merged, rest = tokens, []
            for existing in out:
                if existing & merged:
                    merged = merged | existing
                else:
                    rest.append(existing)
            rest.append(merged)
            out = rest
        return out

    flagged: list[str] = []
    for row in conn.execute(
        "SELECT id, alias FROM contacts WHERE merged_into IS NULL "
        "AND COALESCE(is_deleted, 0) = 0 AND merge_reviewed_at IS NULL"
    ).fetchall():
        names = [a[0] for a in conn.execute(
            "SELECT name FROM contact_aliases WHERE contact_id = ?", (row[0],))]
        if len(groups(names)) >= 2:
            flagged.append(row[1])
    return flagged


def _kept_apart(conn) -> set:
    """
    Contact pairs a human deliberately separated.

    A split says "these records look like one person and are not" — the
    correction an automatic import cannot make for itself. Without this the
    next import re-reads the same source, finds the same shared number, and
    merges them straight back: every review verdict undone by the next run.
    """
    pairs = set()
    for survivor_id, other_id in conn.execute(
        "SELECT m.surviving_id, c.id FROM merge_log m "
        "JOIN contacts c ON c.alias = m.merged_alias "
        "WHERE m.reason LIKE 'split out of %' AND m.undone_at IS NULL"
    ):
        if survivor_id and other_id:
            pairs.add(frozenset((survivor_id, other_id)))
    return pairs


def absorb(conn, survivor_id: int, victim_id: int,
            session_id: str, reason: str) -> None:
    """
    Fold one existing contact into another, reversibly.

    Only reached when a new card proves two rows that were already in the
    database are the same person — e.g. one filed under a phone and one under
    an email, and a third card carries both.
    """
    victim = conn.execute(
        "SELECT * FROM contacts WHERE id = ?", (victim_id,)
    ).fetchone()
    if victim is None or victim_id == survivor_id:
        return

    payload = {
        "contact": dict(victim),
        "identifiers": [dict(r) for r in conn.execute(
            "SELECT * FROM contact_identifiers WHERE contact_id = ?", (victim_id,))],
        "aliases": [dict(r) for r in conn.execute(
            "SELECT * FROM contact_aliases WHERE contact_id = ?", (victim_id,))],
        "sources": [dict(r) for r in conn.execute(
            "SELECT id, contact_id, source_file, card_index FROM contact_sources "
            "WHERE contact_id = ?", (victim_id,))],
        "routes": [dict(r) for r in conn.execute(
            "SELECT * FROM contact_routes WHERE contact_id = ?", (victim_id,))],
    }

    for table in ("contact_identifiers", "contact_aliases",
                  "contact_sources", "contact_routes"):
        # OR IGNORE: the survivor may already hold the same identifier/alias,
        # and the unique indexes are what keep that from becoming a duplicate.
        conn.execute(
            f"UPDATE OR IGNORE {table} SET contact_id = ? WHERE contact_id = ?",
            (survivor_id, victim_id),
        )
        conn.execute(f"DELETE FROM {table} WHERE contact_id = ?", (victim_id,))

    conn.execute(
        "UPDATE contacts SET merged_into = ?, is_deleted = 1, updated_at = ? "
        "WHERE id = ?",
        (survivor_id, _utcnow(), victim_id),
    )
    conn.execute(
        """INSERT INTO merge_log
               (session_id, surviving_id, merged_alias, reason, key_kind,
                key_value, payload, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (session_id, survivor_id, victim["alias"], reason, None, None,
         json.dumps(payload, ensure_ascii=False, default=str), _utcnow()),
    )


def _write_cluster(conn, contact_id: int, cluster: Cluster,
                   source_label: str, now: str) -> None:
    """Attach a cluster's identifiers, aliases and provenance to a contact."""
    for kind, value, raw, is_primary in cluster.identifier_rows():
        conn.execute(
            """INSERT OR IGNORE INTO contact_identifiers
                   (contact_id, kind, value_norm, value_raw, is_primary,
                    source_file, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (contact_id, kind, value, raw, is_primary,
             cluster.source_files()[0] if cluster.source_files() else None, now),
        )
    for name, key, source_file in cluster.aliases():
        conn.execute(
            """INSERT OR IGNORE INTO contact_aliases
                   (contact_id, name, name_key, source_file, is_primary, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (contact_id, name, key, source_file,
             1 if name == cluster.display_name() else 0, now),
        )
    for card in cluster.cards:
        already = conn.execute(
            "SELECT 1 FROM contact_sources WHERE contact_id = ? AND source_file = ? "
            "AND card_index = ?",
            (contact_id, card.source_file, card.card_index),
        ).fetchone()
        if not already:
            conn.execute(
                """INSERT INTO contact_sources
                       (contact_id, source_file, card_index, raw_card, imported_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (contact_id, card.source_file, card.card_index,
                 card.raw.strip(), now),
            )
    # An identifier is what a person IS reachable at; a route is how a message
    # gets there.  Writing both is the point of importing an address book into
    # the same store dispatch uses: `navig dispatch send <alias>` can now reach
    # everyone the import found, instead of the import being a read-only record.
    for kind, value, _raw, _primary in cluster.identifier_rows():
        network = IDENTIFIER_ROUTE_NETWORK.get(kind)
        if network is None:
            continue
        conn.execute(
            """INSERT OR IGNORE INTO contact_routes
                   (contact_id, network, address, priority)
               VALUES (?, ?, ?, 0)""",
            (contact_id, network, value),
        )


def persist(conn, clusters: list[Cluster], session_id: str,
            source_label: str, summary: ImportSummary) -> dict:
    """
    Write clusters to the database, merging into existing people where a hard
    identifier already belongs to someone.

    Returns per-cluster outcomes for the reports.  Never called with archive-tier
    clusters — those are dropped upstream and only ever appear in a report.
    """
    now = _utcnow()
    kept_apart = _kept_apart(conn)
    outcomes: list[dict] = []

    for cluster in clusters:
        summary.total += 1
        alias = cluster.alias()
        if not alias:
            summary.skipped_no_uid += 1
            continue

        try:
            existing = _existing_ids_for(conn, cluster)

            if existing:
                survivor = min(existing)
                for victim in existing:
                    if victim == survivor:
                        continue
                    if frozenset((survivor, victim)) in kept_apart:
                        # A human split these two apart. What each already
                        # owns stays theirs (the unique index sees to that);
                        # re-merging would undo the correction.
                        continue
                    absorb(conn, survivor, victim, session_id,
                           f"shared identifier via {alias}")
                contact_id = survivor
                summary.duplicates += 1
                action = "attached"
            else:
                row = conn.execute(
                    "SELECT id FROM contacts WHERE alias = ?", (alias,)
                ).fetchone()
                if row:
                    contact_id = row[0]
                    summary.duplicates += 1
                    action = "attached"
                else:
                    cur = conn.execute(
                        """INSERT INTO contacts
                               (alias, display_name, country, city,
                                source_profile, is_deleted, created_at, updated_at,
                                tier, primary_phone, primary_email, birthday,
                                org, job_title, is_org)
                           VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                        (alias, cluster.display_name() or "",
                         cluster.country(), cluster.city(), source_label, now, now,
                         cluster.tier(),
                         cluster.phones()[0][0] if cluster.phones() else None,
                         cluster.emails()[0][0] if cluster.emails() else None,
                         cluster.birthday(), cluster.org(), cluster.title()),
                    )
                    contact_id = cur.lastrowid
                    summary.inserted += 1
                    action = "inserted"

            _write_cluster(conn, contact_id, cluster, source_label, now)
            _promote(conn, contact_id, cluster, now)

            outcomes.append({
                "alias": alias, "contact_id": contact_id, "action": action,
                "tier": cluster.tier(), "name": cluster.display_name(),
                "cards": len(cluster.cards),
                "aliases": [a[0] for a in cluster.aliases()],
                "phones": [p[0] for p in cluster.phones()],
                "emails": [e[0] for e in cluster.emails()],
                "handles": [f"{k}:{v}" for k, v, _ in cluster.handles()],
                "sources": cluster.source_files(),
            })
        except Exception as exc:  # one bad cluster must not abort the import
            summary.errors += 1
            outcomes.append({"alias": alias, "action": "error", "error": str(exc)})

    return {"outcomes": outcomes}


def _promote(conn, contact_id: int, cluster: Cluster, now: str) -> None:
    """
    Raise an existing contact's tier and back-fill blank fields.

    This is how a lead becomes a real contact: the day a card turns up carrying
    a phone for someone previously known only by a handle, they move to
    'verified' rather than staying filed as unreachable.
    """
    row = conn.execute(
        "SELECT tier, primary_phone, primary_email, display_name, city, country, "
        "birthday, org, job_title FROM contacts WHERE id = ?", (contact_id,)
    ).fetchone()
    if row is None:
        return

    ranks = {"verified": 3, "email": 2, "handle": 1, "archive": 0}
    best_tier = row["tier"] or "archive"
    if ranks.get(cluster.tier(), 0) > ranks.get(best_tier, 0):
        best_tier = cluster.tier()

    phone = row["primary_phone"] or (
        cluster.phones()[0][0] if cluster.phones() else None)
    email = row["primary_email"] or (
        cluster.emails()[0][0] if cluster.emails() else None)
    # A contact holding a phone is verified regardless of which card brought it.
    if phone:
        best_tier = "verified"
    elif email and best_tier not in ("verified",):
        best_tier = "email"

    conn.execute(
        """UPDATE contacts
              SET tier = ?, primary_phone = ?, primary_email = ?,
                  display_name = COALESCE(NULLIF(display_name, ''), ?),
                  city      = COALESCE(city, ?),
                  country   = COALESCE(country, ?),
                  birthday  = COALESCE(birthday, ?),
                  org       = COALESCE(org, ?),
                  job_title = COALESCE(job_title, ?),
                  updated_at = ?
            WHERE id = ?""",
        (best_tier, phone, email, cluster.display_name() or None,
         cluster.city(), cluster.country(), cluster.birthday(),
         cluster.org(), cluster.title(), now, contact_id),
    )


@dataclass
class UndoOutcome:
    """What happened when a merge was reversed."""

    status: str          # ok | missing | already_undone | unreadable
    detail: str = ""
    moved: int = 0
    blocked: list[str] = field(default_factory=list)


def undo_merge(conn, merge_id: int) -> UndoOutcome:
    """
    Reverse one logged merge, restoring the contact that was absorbed.

    Identifiers are globally unique, so one can only sit on a single contact:
    anything the survivor turned out to own as well stays with the survivor and
    is reported in ``blocked`` rather than being silently taken back.
    """
    row = conn.execute(
        "SELECT * FROM merge_log WHERE id = ?", (merge_id,)
    ).fetchone()
    if row is None:
        return UndoOutcome("missing")
    if row["undone_at"]:
        return UndoOutcome("already_undone", detail=str(row["undone_at"]))

    try:
        payload = json.loads(row["payload"] or "{}")
    except json.JSONDecodeError:
        return UndoOutcome("unreadable")

    contact = payload.get("contact") or {}
    victim_id = contact.get("id")
    if not victim_id:
        return UndoOutcome("unreadable")

    now = _utcnow()
    if conn.execute("SELECT 1 FROM contacts WHERE id = ?", (victim_id,)).fetchone():
        conn.execute(
            "UPDATE contacts SET merged_into = NULL, is_deleted = 0, "
            "updated_at = ? WHERE id = ?", (now, victim_id),
        )
    else:
        cols = [c for c in contact if c != "id"]
        conn.execute(
            f"INSERT INTO contacts (id, {', '.join(cols)}) "
            f"VALUES (?{', ?' * len(cols)})",
            [victim_id] + [contact[c] for c in cols],
        )

    moved = 0
    blocked: list[str] = []
    for ident in payload.get("identifiers", []):
        cur = conn.execute(
            "UPDATE contact_identifiers SET contact_id = ? "
            "WHERE kind = ? AND value_norm = ? AND contact_id = ?",
            (victim_id, ident["kind"], ident["value_norm"], row["surviving_id"]),
        )
        if cur.rowcount:
            moved += 1
        else:
            blocked.append(f"{ident['kind']}:{ident['value_norm']}")

    for alias in payload.get("aliases", []):
        conn.execute(
            "UPDATE OR IGNORE contact_aliases SET contact_id = ? "
            "WHERE name = ? AND contact_id = ?",
            (victim_id, alias.get("name"), row["surviving_id"]),
        )
    for src in payload.get("sources", []):
        conn.execute("UPDATE contact_sources SET contact_id = ? WHERE id = ?",
                     (victim_id, src["id"]))
    for route in payload.get("routes", []):
        conn.execute("UPDATE OR IGNORE contact_routes SET contact_id = ? "
                     "WHERE id = ?", (victim_id, route["id"]))

    conn.execute("UPDATE merge_log SET undone_at = ? WHERE id = ?", (now, merge_id))
    return UndoOutcome("ok", detail=str(contact.get("alias") or victim_id),
                       moved=moved, blocked=blocked)

@dataclass
class SplitOutcome:
    """What happened when an identifier was pulled onto its own contact."""

    status: str          # ok | bad_spec | not_held | would_strand | taken
    detail: str = ""


def split_identifier(conn, alias: str, spec: str,
                     new_name: Optional[str] = None) -> SplitOutcome:
    """
    Move one identifier off a contact onto a contact of its own.

    The counterpart to a merge, and the operation reviewing an automatic import
    actually needs. The failure mode of an address-book import is not a wrong
    name — it is one record listing somebody else's number, so two people are
    read as one from the start. ``undo_merge`` cannot help there, because no
    merge was ever logged.

    An address belongs to exactly one contact, so the identifier *moves*: it
    leaves the original and arrives on the new one, and nothing is duplicated.
    """
    if ":" not in spec:
        return SplitOutcome("bad_spec")
    kind, _, value = spec.partition(":")
    kind, value = kind.strip().lower(), value.strip().lower()

    source = conn.execute(
        "SELECT * FROM contacts WHERE alias = ? COLLATE NOCASE", (alias,)
    ).fetchone()
    if source is None:
        return SplitOutcome("not_held", detail="")

    ident = conn.execute(
        "SELECT * FROM contact_identifiers WHERE contact_id = ? AND kind = ? "
        "AND value_norm = ?", (source["id"], kind, value),
    ).fetchone()
    if ident is None:
        held = [f"{r[0]}:{r[1]}" for r in conn.execute(
            "SELECT kind, value_norm FROM contact_identifiers WHERE contact_id = ?",
            (source["id"],))]
        return SplitOutcome("not_held", detail=", ".join(held))

    placeholders = ", ".join("?" * len(HARD_KINDS))
    remaining = conn.execute(
        f"SELECT COUNT(*) FROM contact_identifiers WHERE contact_id = ? "
        f"AND kind IN ({placeholders})",
        (source["id"], *HARD_KINDS),
    ).fetchone()[0]
    if remaining <= 1:
        return SplitOutcome("would_strand")

    prefix = _UID_PREFIX.get(kind, "x")
    new_alias = f"{prefix}:{value.lstrip('+')}"
    if conn.execute("SELECT 1 FROM contacts WHERE alias = ? COLLATE NOCASE",
                    (new_alias,)).fetchone():
        return SplitOutcome("taken", detail=new_alias)

    label = new_name or value
    now = _utcnow()
    cur = conn.execute(
        """INSERT INTO contacts (alias, display_name, city, country, source_profile,
                                 is_deleted, created_at, updated_at, tier,
                                 primary_phone, primary_email)
           VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)""",
        (new_alias, label, source["city"], source["country"],
         source["source_profile"], now, now,
         "verified" if kind == "phone" else ("email" if kind == "email" else "handle"),
         value if kind == "phone" else None,
         value if kind == "email" else None),
    )
    new_id = cur.lastrowid

    conn.execute(
        "UPDATE contact_identifiers SET contact_id = ?, is_primary = 1 WHERE id = ?",
        (new_id, ident["id"]),
    )
    # The route that mirrored this identifier belongs with it.
    network = IDENTIFIER_ROUTE_NETWORK.get(kind)
    if network:
        conn.execute(
            "UPDATE OR IGNORE contact_routes SET contact_id = ? "
            "WHERE contact_id = ? AND network = ? AND address = ?",
            (new_id, source["id"], network, value),
        )

    moved = 0
    if new_name:
        moved = conn.execute(
            "UPDATE OR IGNORE contact_aliases SET contact_id = ? "
            "WHERE contact_id = ? AND name = ?",
            (new_id, source["id"], new_name),
        ).rowcount
    if not moved:
        conn.execute(
            """INSERT OR IGNORE INTO contact_aliases
                   (contact_id, name, name_key, source_file, is_primary, created_at)
               VALUES (?, ?, ?, 'split', 1, ?)""",
            (new_id, label, name_key(label), now),
        )

    _refresh_primaries(conn, source["id"])
    # Splitting says the earlier "one person" verdict was wrong, so the
    # contact goes back on the review list rather than staying signed off.
    conn.execute("UPDATE contacts SET merge_reviewed_at = NULL WHERE id = ?",
                 (source["id"],))
    conn.execute(
        """INSERT INTO merge_log (session_id, surviving_id, merged_alias, reason,
                                  key_kind, key_value, payload, created_at)
           VALUES ('manual', ?, ?, ?, ?, ?, ?, ?)""",
        (source["id"], new_alias, f"split out of {alias}", kind, value,
         json.dumps({"split_from": alias, "new_alias": new_alias,
                     "identifier": spec, "name": label}, ensure_ascii=False), now),
    )
    return SplitOutcome("ok", detail=new_alias)


def _refresh_primaries(conn, contact_id: int) -> None:
    """Re-point primary_phone / primary_email at identifiers still held."""
    phone = conn.execute(
        "SELECT value_norm FROM contact_identifiers WHERE contact_id = ? "
        "AND kind = 'phone' ORDER BY is_primary DESC, id LIMIT 1", (contact_id,)
    ).fetchone()
    email = conn.execute(
        "SELECT value_norm FROM contact_identifiers WHERE contact_id = ? "
        "AND kind = 'email' ORDER BY is_primary DESC, id LIMIT 1", (contact_id,)
    ).fetchone()
    tier = "verified" if phone else ("email" if email else "handle")
    conn.execute(
        "UPDATE contacts SET primary_phone = ?, primary_email = ?, tier = ?, "
        "updated_at = ? WHERE id = ?",
        (phone[0] if phone else None, email[0] if email else None, tier,
         _utcnow(), contact_id),
    )
