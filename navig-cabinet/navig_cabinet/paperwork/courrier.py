"""What a letter *says*: who sent it, by when, under which reference, asking for what.

Everything here is regex over the extracted (OCR'd) text — no model, no network. That
is a deliberate floor, not a ceiling: the fields a deadline radar needs from French
administrative mail are formulaic ("avant le 30 septembre 2026", "sous 15 jours",
"N° de dossier : …"), and a rule that finds them can be read and corrected by the
person whose CAF letter it is.

Two privacy rules shape the output:

* The **NIR** (social-security number) is never extracted. It identifies the person,
  not the dossier, and has no business in a plan, a ledger or a Telegram message.
* A dossier ``reference`` is captured raw for the plan (which lives inside the space)
  but the ledger keeps only ``reference_hash`` — enough to recognise the same dossier
  twice, useless to anyone reading the ledger.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from . import signals as S

_HEAD = 6000  # how much of the document the field finders read


@dataclass
class Courrier:
    """The dossier-level facts of one letter. Every field may be empty."""

    emetteur: str = ""  # slug, e.g. "caf" — goes into the filename
    emetteur_label: str = ""  # display, e.g. "CAF" — goes into the ledger / Telegram
    echeance: str = ""  # ISO date of the deadline, "" when none was found
    echeance_source: str = ""  # the phrase that produced it (audit trail)
    reference: str = ""  # raw dossier reference, plan only
    action: str = "info"  # payer | fournir | declarer | repondre | info
    channel: str = ""  # routes.yaml channel id, e.g. "#logement"
    objet: str = ""  # the letter's own "Objet :" line, for naming a photo

    @property
    def reference_hash(self) -> str:
        return reference_hash(self.reference)


# ── Émetteur ────────────────────────────────────────────────────────────────

_ORG_LABELS: dict[str, str] = {slug: label for slug, label, _ in S.ORGANISMS}


def organism_label(slug: str) -> str:
    """Display name for an organism slug ("caf" -> "CAF"); "" for unknown/empty."""
    return _ORG_LABELS.get((slug or "").strip().lower(), "")


def reference_hash(reference: str) -> str:
    """Short, stable hash of a raw dossier reference — what the ledger stores."""
    if not reference:
        return ""
    return hashlib.sha256(reference.encode("utf-8")).hexdigest()[:12]


def find_emetteur(text: str, filename: str = "") -> tuple[str, str]:
    """``(slug, label)`` of the first known organism in the letter head, or ``("", "")``.

    The filename is searched too: a scan the operator already named ``caf-…`` is
    evidence, and an image-only page that OCR could not read has nothing else.
    """
    from .classify import fold

    hay = fold(filename) + "\n" + fold(text[:_HEAD])
    for slug, label, pattern in S.ORGANISMS:
        if pattern.search(hay):
            return slug, label
    return "", ""


# ── Échéance ────────────────────────────────────────────────────────────────


def _iso(d: date) -> str:
    return d.isoformat()


def _from_iso(s: str) -> date | None:
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def find_echeance(
    text: str, doc_date: str = "", *, today: date | None = None
) -> tuple[str, str]:
    """``(iso_date, source_phrase)`` of the deadline, or ``("", "")``.

    Order of trust: an absolute date right after a deadline label; then a relative
    delay ("sous 15 jours") anchored on the letter's own date; nothing else. A relative
    delay with no letter date is unresolvable and reported as such rather than guessed
    from today's date — the letter may have sat in a pile for a week.
    """
    from .classify import _dates_in, fold

    folded = fold(text[:_HEAD])
    anchor = _from_iso(doc_date) if len(doc_date) == 10 else None
    floor = anchor or (today or date.today()) - timedelta(days=365)

    # 1. "avant le 30 septembre 2026"
    candidates: list[tuple[date, str]] = []
    for m in S.DEADLINE_LABEL.finditer(folded):
        window = folded[m.end() : m.end() + 60]
        for iso in _dates_in(window):
            d = _from_iso(iso)
            if d is None:
                continue
            # A deadline before the letter itself is a parse error, not a deadline.
            if anchor and d < anchor:
                continue
            if d < floor:
                continue
            phrase = folded[m.start() : m.end() + 60].split("\n")[0].strip()
            candidates.append((d, phrase[:80]))
            break
    if candidates:
        d, phrase = min(candidates, key=lambda c: c[0])
        return _iso(d), phrase

    # 2. "sous 15 jours" — needs the letter date to mean anything.
    m = S.DEADLINE_RELATIVE.search(folded)
    if m:
        phrase = m.group(0).strip()
        if anchor is None:
            return "", f"relative:{phrase}"
        if m.group(3):  # quinzaine / huitaine
            days = 15 if m.group(3).startswith("quinz") else 8
        else:
            raw, unit = m.group(1), m.group(2)
            n = int(raw) if raw.isdigit() else S.FR_NUMBER_WORDS.get(raw, 0)
            if not n:
                return "", ""
            if unit.startswith("semaine"):
                days = n * 7
            elif unit.startswith("mois"):
                days = n * 30
            else:
                days = n
        return _iso(anchor + timedelta(days=days)), phrase

    return "", ""


# ── Référence ───────────────────────────────────────────────────────────────

_NIR_LIKE = re.compile(r"^\d{13,15}$")


def find_reference(text: str) -> str:
    """The first labelled dossier/contract/customer reference, or "".

    A 13–15 digit capture is discarded: that shape is the NIR, and a label like
    "N° de sécurité sociale" is deliberately absent from the pattern, but a bare
    "N° :" next to one must not smuggle it in either.
    """
    head = text[:_HEAD]
    for m in S.REFERENCE_LABEL.finditer(head):
        value = re.sub(r"\s+", " ", m.group(1)).strip(" .-/")
        digits = re.sub(r"\D", "", value)
        if _NIR_LIKE.match(digits) and len(digits) == len(value.replace(" ", "")):
            continue
        if len(value) < 4:
            continue
        return value[:40]
    return ""


# ── Objet ───────────────────────────────────────────────────────────────────

_OBJET = re.compile(
    r"(?im)^\s*(?:objet|sujet|concerne|ref\.?\s*objet)\s*:\s*(.{4,120})$"
)
# A camera or scanner name says nothing about the letter; the objet line does.
GENERIC_STEM = re.compile(
    r"^(?:img|dsc|dscn|pxl|scan|scanned|photo|image|document|capture|screenshot|numerisation|"
    r"cameraphoto|whatsapp image|signal-)[\w .-]*$",
    re.I,
)


def find_objet(text: str) -> str:
    """The letter's "Objet :" line, cleaned, or ""."""
    m = _OBJET.search(text[:_HEAD])
    if not m:
        return ""
    value = re.sub(r"\s+", " ", m.group(1)).strip(" .:-")
    return value[:100]


def is_generic_stem(stem: str) -> bool:
    return bool(GENERIC_STEM.match(stem.strip()))


# ── Action ──────────────────────────────────────────────────────────────────


def find_action(text: str) -> str:
    from .classify import fold

    folded = fold(text[:_HEAD])
    for name, pattern in S.ACTIONS:
        if pattern.search(folded):
            return name
    return "info"


# ── Channel (the space's own routing taxonomy) ─────────────────────────────

# bucket → routes.yaml channel, when the space's routes.yaml uses the paperwork
# channel set. A keyword hit in routes.yaml itself outranks this fallback.
_BUCKET_CHANNEL = {
    "logement": "#logement",
    "energie-telecom": "#logement",
    "sante": "#sante",
    "identite": "#identite",
    "sejour": "#identite",
}


def load_channels(space_root: Path) -> list[tuple[str, list[str]]]:
    """``[(channel_id, [keywords…])]`` from ``<space>/.navig/inbox/routes.yaml``; ``[]`` if none."""
    try:
        from navig.inbox.routes_loader import load

        cfg = load(space_root)
    except Exception:  # noqa: BLE001 — no routes file, or core loader unavailable
        return []
    if cfg is None:
        return []
    out: list[tuple[str, list[str]]] = []
    for ch in getattr(cfg, "channels", []) or []:
        kws = [
            str(k).strip().lower()
            for k in (getattr(ch, "keywords", []) or [])
            if str(k).strip()
        ]
        if ch.id and kws:
            out.append((str(ch.id), kws))
    return out


def find_channel(
    text: str,
    filename: str,
    bucket: str,
    channels: list[tuple[str, list[str]]],
    *,
    has_echeance: bool = False,
) -> str:
    """The routes.yaml channel with the most keyword hits; else the bucket's default."""
    from .classify import fold

    hay = fold(filename) + " " + fold(text[:_HEAD])
    best, best_hits = "", 0
    for cid, kws in channels:
        hits = sum(1 for k in kws if k and fold(k) in hay)
        if hits > best_hits:
            best, best_hits = cid, hits
    if best:
        return best
    if has_echeance and any(c == "#echeances" for c, _ in channels):
        return "#echeances"
    return _BUCKET_CHANNEL.get(bucket, "#courrier" if channels else "")


# ── All together ────────────────────────────────────────────────────────────


def read_courrier(
    text: str,
    filename: str,
    *,
    doc_date: str = "",
    bucket: str = "",
    channels: list[tuple[str, list[str]]] | None = None,
) -> Courrier:
    slug, label = find_emetteur(text, filename)
    echeance, source = find_echeance(text, doc_date)
    c = Courrier(
        emetteur=slug,
        emetteur_label=label,
        echeance=echeance,
        echeance_source=source,
        reference=find_reference(text),
        action=find_action(text),
        objet=find_objet(text),
    )
    c.channel = find_channel(
        text, filename, bucket, channels or [], has_echeance=bool(echeance)
    )
    return c
