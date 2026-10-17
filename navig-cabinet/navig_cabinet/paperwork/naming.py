"""Where a document goes, and what it is called when it gets there.

Convention: ISO date first so a directory sorts chronologically in any file manager,
then kebab-case ASCII. The one exception is the invoice identifier, which is kept
**uppercase and verbatim** — it is a legal identifier under the French sequential-
numbering rule and has to stay greppable exactly as printed on the document.

The tree fills the folders the space already declared and left empty
(``finance/{invoices,quotes,tax,expenses,reports}``, ``clients``, ``contracts``)
rather than inventing a parallel structure beside them.
"""

from __future__ import annotations

from pathlib import Path

from .names import repair, slug

# Classes that never receive a destination. A handoff row carrying one would be a
# structural bug — `plan.py` asserts this, and a test locks it.
NO_DESTINATION = frozenset({"personal-handoff", "business-handoff"})

# Personal profile: one folder per family, matching the paperwork space's own
# `personal/<bucket>/` tree (its CLAUDE.md declares these; nothing is invented).
COURRIER_PREFIX = "courrier-"

_TAX_TOPICS = (
    ("urssaf", "urssaf"),
    ("acre", "acre"),
    ("radiation", "urssaf"),
    ("cotisation", "cotisations"),
    ("impot", "impots"),
    ("revenus", "impots"),
    ("2042", "impots"),
    ("prelevement", "impots"),
    ("cfe", "impots"),
    ("declaration", "urssaf"),
)


def _year(doc_date: str) -> str:
    return doc_date[:4] if len(doc_date) >= 4 and doc_date[:4].isdigit() else "undated"


def _date_prefix(doc_date: str) -> str:
    return f"{doc_date}-" if len(doc_date) == 10 else ""


def _subject(stem: str, *, limit: int = 60) -> str:
    """A slug of the original name, for documents with no better identifier."""
    return slug(repair(stem), max_len=limit)


def _tax_topic(stem: str, text_hint: str = "") -> str:
    hay = (stem + " " + text_hint).lower()
    for token, topic in _TAX_TOPICS:
        if token in hay:
            return topic
    return "divers"


def destination(row) -> str:
    """POSIX path relative to the space root. "" when the class takes no destination."""
    cls = row.doc_class
    if cls in NO_DESTINATION:
        return ""

    src = Path(row.src)
    ext = src.suffix.lower()
    stem = src.stem
    year = _year(row.doc_date)
    datep = _date_prefix(row.doc_date)
    subject = _subject(stem)
    # A short hash keeps two same-day documents from the same vendor apart, and gives
    # a non-Latin filename (a Cyrillic scan) something addressable to be called.
    short = (row.sha256 or "")[:8] or "nohash"
    party = slug(row.counterparty, max_len=40)

    if cls.startswith(COURRIER_PREFIX):
        from .courrier import is_generic_stem

        bucket = slug(cls[len(COURRIER_PREFIX) :], max_len=30) or "unsorted"
        sender = slug(getattr(row, "emetteur", "") or "", max_len=30)
        head = f"{sender}-" if sender else ""
        objet = slug(getattr(row, "objet", "") or "", max_len=60)
        # `IMG_2026.jpg` says nothing; the letter's own "Objet :" line does.
        body = (
            objet if (objet and is_generic_stem(stem)) else (subject or objet or short)
        )
        return f"personal/{bucket}/{year}/{datep}{head}{body}{ext}"

    if cls == "invoice-issued":
        # The identifier is the name. Uppercase, verbatim, never slugged.
        ident = row.doc_id or f"INV-UNKNOWN-{short}"
        tail = f"-{party}" if party else ""
        return f"finance/invoices/issued/{year}/{datep}{ident}{tail}{ext}"

    if cls == "invoice-received":
        vendor = party or "vendeur-inconnu"
        return f"finance/invoices/received/{year}/{datep}{vendor}-{short}{ext}"

    if cls == "quote-issued":
        ident = row.doc_id.upper() if row.doc_id else subject or short
        tail = f"-{party}" if party else ""
        return f"finance/quotes/{year}/{datep}{ident}{tail}{ext}"

    if cls == "tax-social":
        return f"finance/tax/{year}/{_tax_topic(stem)}/{datep}{subject or short}{ext}"

    if cls == "bank":
        return f"finance/reports/bank/{year}/{datep}{subject or short}{ext}"

    if cls == "contract":
        tail = f"{party}-" if party else ""
        return f"contracts/{year}/{datep}{tail}{subject or short}{ext}"

    if cls == "client-material":
        client = party or "non-attribue"
        return f"clients/{client}/materials/{datep}{subject or short}{ext}"

    if cls == "company-legal":
        return f"ops/legal/{year}/{datep}{subject or short}{ext}"

    if cls == "payroll-employment":
        return f"ops/payroll/{year}/{datep}{subject or short}{ext}"

    # `unknown` and anything a human re-labels to something unmapped: keep the repaired
    # original name so nothing is silently renamed on the way to manual triage.
    safe = slug(repair(stem), max_len=100) or short
    return f"docs/unsorted/{year}/{safe}{ext}"
