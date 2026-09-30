"""Decide what a document is, and whether it is the company's at all.

Two stages, in this order, and the order is the whole design:

1. **Veto.** Does this document belong to the person rather than the business? A hit
   ends classification immediately with ``personal-handoff``. It is not a score and
   nothing can outweigh it, because the documents that most need catching are exactly
   the ones that score well: ``facture-mizerny-sanmartin-serg-20231214-1202.pdf`` is
   named *facture*, sits in *Factures-Invoices*, and is a dental fee note.

2. **Score.** Each signal carries "how likely is this class given only this signal",
   and a class's signals combine by noisy-OR, so corroboration raises confidence
   without ever exceeding certainty. Top class wins; ties within 0.05 fall to review.
   One unambiguous signal — the ``INV-`` series in a filename — is deliberately
   decisive on its own, because two of the operator's issued invoices are image-only
   scans that extract to zero text.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import courrier as C
from . import signals as S

# Confidence bands.
#
# 0.70, not 0.80. The weights below are independent pieces of evidence combined with
# noisy-OR, so a class supported by one strong signal lands near 0.65 and a class with
# two corroborating signals lands near 0.83. An 0.80 bar meant every single-family
# class — tax, contract, quote, bank, payroll — could never clear it no matter how
# unambiguous the document was: a first real run put 515 of 812 documents into review,
# which is not caution, it is the tool declining to do its job.
#
# What makes 0.70 safe is that it is not the thing protecting anything important. The
# ownership veto is absolute and separate, so the worst outcome here is a business
# document filed under the wrong business folder — visible in index.md, and reversible
# with `undo`.
MIGRATE_AT = 0.70
REVIEW_AT = 0.45
TIE_WINDOW = 0.05


@dataclass
class PaperClass:
    doc_class: str = "unknown"
    confidence: float = 0.0
    signals: list[str] = field(default_factory=list)
    veto_subclass: str | None = None
    doc_id: str = ""
    doc_date: str = ""
    counterparty: str = ""
    expense_category: str = ""
    amount: str = ""
    # Personal profile only — the dossier facts of a letter (see ``courrier.py``).
    bucket: str = ""
    emetteur: str = ""
    echeance: str = ""
    echeance_source: str = ""
    reference: str = ""
    action_requise: str = ""
    channel: str = ""
    objet: str = ""

    @property
    def is_handoff(self) -> bool:
        return self.veto_subclass is not None

    def as_classify_result(self):
        """Adapt to ``navig.inbox.classifier.ClassifyResult`` for interop.

        Not used by this plugin's own pipeline (we do not route through
        ``InboxRouter`` — see the module docstring in ``apply.py``), but it lets a
        caller hand our verdict to core inbox tooling without a translation layer.
        """
        from navig.inbox.classifier import ClassifyResult

        return ClassifyResult(
            category=self.doc_class,
            confidence=self.confidence,
            method="paperwork-rules",
            explanation="+".join(self.signals),
        )


def fold(text: str) -> str:
    """Lowercase, strip accents. Every pattern in ``signals`` matches against this."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


# ── Field extraction ────────────────────────────────────────────────────────


def _dates_in(haystack: str) -> list[str]:
    """Every plausible ISO date in *haystack*, in the order they appear."""
    found: list[tuple[int, str]] = []
    folded = fold(haystack)

    for m in S.FR_DATE.finditer(folded):
        day, month, year = int(m.group(1)), S.FR_MONTHS[m.group(2).lower()], int(m.group(3))
        try:
            found.append((m.start(), date(year, month, day).isoformat()))
        except ValueError:
            continue

    for pattern, order in S.DATE_PATTERNS:
        for m in pattern.finditer(haystack):
            a, b, c = (int(g) for g in m.groups())
            year, month, day = (a, b, c) if order == "ymd" else (c, b, a)
            if year < S.MIN_YEAR or year > date.today().year + 1:
                continue
            try:
                found.append((m.start(), date(year, month, day).isoformat()))
            except ValueError:
                continue

    return [iso for _, iso in sorted(found)]


def _parse_date(text: str, filename: str, *, prefer_year: int | None = None) -> str:
    """Best ISO date for the document. Returns "" — or a bare year — when unsure.

    Order of trust: a date next to an explicit label, then any date matching a year we
    already know from the document's own identifier, then the first plausible date.

    *prefer_year* is authoritative when supplied. An invoice numbered ``INV-000017-21``
    was issued in 2021 by definition, so a stray ``04/08/2008`` elsewhere on the page
    cannot outrank it — that exact case filed a 2021 invoice under 2008. When no date
    on the page agrees with the identifier, a bare year is returned rather than a
    confidently wrong day.
    """
    head = text[:6000]

    labelled: list[str] = []
    for m in S.DATE_LABEL.finditer(head):
        labelled += _dates_in(head[m.end(): m.end() + 40])

    candidates = labelled + _dates_in(head) + _dates_in(filename)

    if prefer_year is not None:
        for iso in candidates:
            if iso.startswith(str(prefer_year)):
                return iso
        return str(prefer_year)

    return candidates[0] if candidates else ""


def _own_name_near(text: str, start: int, window: int = 200) -> bool:
    chunk = fold(text[start : start + window])
    return any(n in chunk for n in S.OWN_NAMES)


def _foreign_siret(text: str) -> str:
    """A labelled SIRET/SIREN that is NOT ours. "" when there is none."""
    for m in S.FOREIGN_SIRET.finditer(text):
        num = _digits(m.group(1))
        if num and not num.startswith("753204742"):
            return num
    return ""


def _vendor(text: str, filename: str) -> str:
    hay = fold(filename + " " + text[:3000])
    for token in S.VENDORS:
        if token in hay:
            return token
    return ""


def _client(text: str, filename: str) -> str:
    """The counterparty a document was issued TO.

    A known name first, then whatever the document's own addressee block says. The
    fallback matters: a hardcoded list only ever knows yesterday's clients, and the
    most recent invoice in this corpus is addressed to a company the list had never
    heard of — it came back blank while the PDF plainly said "Bill To: FETCH NETWORK".
    """
    hay = fold(filename + " " + text[:3000])
    for token in S.CLIENTS:
        if token in hay:
            return token
    return _addressee(text)


def _addressee(text: str) -> str:
    """The first plausible name after a "Bill To" / "Destinataire" marker."""
    for m in S.BILL_TO.finditer(text[:6000]):
        for line in text[m.end(): m.end() + 240].splitlines():
            candidate = line.strip(" :\t-—")
            if not candidate or len(candidate) < 3:
                continue
            folded = fold(candidate)
            if any(n in folded for n in S.OWN_NAMES):
                return ""  # our own name here means the document was issued to us
            if S.ADDRESS_LINE.match(candidate) or len(candidate) > 60:
                continue
            return candidate
    return ""


def _amount(text: str) -> str:
    """The invoice's own total. Returns "" rather than a number it cannot justify.

    Two things make this harder than it looks, and both were live in the archive:

    * A PDF text layer breaks words wherever the page wrapped, so a real invoice
      arrives as "Sub T\notal €2000T\notal €2000" and no pattern anchored on the word
      "total" can see it. Removing whitespace repairs that, and joins a French
      thousands space ("4 000,00") for free.
    * The largest or last figure on the page is often not this invoice's. A deposit
      invoice states the *project* total in a note — "facture d'acompte sur le montant
      total de 3700" — while billing 1.110,00. So labels are tried most-specific
      first, and the FIRST match of the best available label wins.

    In a financial register a wrong number is worse than a blank, so anything that
    does not look like money is discarded.
    """
    flat = re.sub(r"\s+", "", fold(text[:8000]))
    for _label, pattern in S.AMOUNT_LABELS:
        for m in pattern.finditer(flat):
            value = _clean_amount(m.group(1))
            if value:
                return value
    for a, b in S.AMOUNT_CURRENCY.findall(flat):
        value = _clean_amount(a or b)
        if value:
            return value
    return ""


def _clean_amount(raw: str) -> str:
    """Keep *raw* only if it reads as money. "" when it does not."""
    value = (raw or "").strip(".,")
    if not value or not value[0].isdigit():
        return ""
    digits = re.sub(r"[^\d]", "", value)
    if len(digits) < 2:
        return ""                       # a bare "1" is a line number, not a total
    # A trailing separator group is either cents (2 digits) or a thousands group
    # (3 digits). Anything else — "1.2", "1.23456" — is not a written amount.
    tail = re.search(r"[.,](\d+)$", value)
    if tail and len(tail.group(1)) not in (2, 3):
        return ""
    return value


# ── Stage 1: the veto ───────────────────────────────────────────────────────


def check_veto(text: str, filename: str) -> str | None:
    """Return the veto subclass, or None. Checked before any scoring.

    Filename patterns are consulted separately from content, because scanned identity
    documents are image-only — ``Carte_Identite.pdf`` extracts to zero characters, so
    a content-only veto never sees it.

    A ``SOFT_VETO`` subclass yields to a strong business anchor. That override exists
    because a URSSAF attestation and a payslip both carry the operator's personal
    details and are still, unambiguously, the company's paperwork. ``health``,
    ``thirdparty`` and ``secret`` are never overridable.
    """
    fname = fold(filename)
    body = fold(text[:8000])
    hay = fname + "\n" + body

    hit: str | None = None
    for subclass, pattern in S.VETO:
        if pattern.search(hay):
            hit = subclass
            break
    if hit is None:
        for subclass, pattern in S.VETO_FILENAME:
            if pattern.search(fname):
                hit = subclass
                break
    if hit is None:
        return None

    if hit in S.SOFT_VETO and S.BUSINESS_ANCHOR.search(hay):
        return None
    return hit


# ── Stage 2: scoring ────────────────────────────────────────────────────────


def _combine(weights: list[float]) -> float:
    """Noisy-OR: independent evidence, none of it individually conclusive.

    Plain addition made the score meaningless — four weak signals summed past 1.0 while
    one decisive signal stayed at its face value — so the number could not be compared
    against a threshold. Here each weight is "how likely is this class *given only this
    signal*", and the combination is the probability that at least one is right.
    Corroborating evidence raises confidence without ever exceeding certainty.
    """
    remaining = 1.0
    for w in weights:
        remaining *= 1.0 - max(0.0, min(w, 0.99))
    return 1.0 - remaining


def _score(text: str, filename: str) -> tuple[dict[str, float], list[str], dict[str, str]]:
    """Accumulate per-class evidence. Returns (scores, signal names, extracted fields)."""
    evidence: dict[str, list[float]] = {}
    hits: list[str] = []
    fields: dict[str, str] = {}
    stem = Path(filename).stem
    ftext = fold(text)
    fname = fold(filename)

    def add(cls: str, weight: float, label: str) -> None:
        evidence.setdefault(cls, []).append(weight)
        hits.append(label)

    # — invoice-issued —
    m_stem = S.INV_SERIES.search(stem) or S.INV_SERIES.search(filename)
    if m_stem:
        # A structured, self-identifying, unique series id. Decisive on its own —
        # it has to be, because two of the real issued invoices are image-only
        # scans whose only readable signal is the filename.
        add("invoice-issued", 0.85, "fn:inv-series")
        fields["doc_id"] = f"INV-{m_stem.group(1)}-{m_stem.group(2)}"
    m_text = S.INV_SERIES.search(text)
    if m_text:
        add("invoice-issued", 0.60, "txt:inv-series")
        fields.setdefault("doc_id", f"INV-{m_text.group(1)}-{m_text.group(2)}")

    has_siren = bool(S.SIREN.search(text))
    has_invoice_word = bool(S.INVOICE_WORD.search(ftext) or S.INVOICE_WORD.search(fname))
    if has_siren and has_invoice_word:
        # Strict conjunction: SIREN alone only means "a document about our company".
        # Three administrative letters in Factures-Invoices carry it and are not invoices.
        add("invoice-issued", 0.45, "txt:siren+invoice")
    if S.TVA_EXEMPT.search(ftext) and S.TVA_293B.search(ftext):
        add("invoice-issued", 0.35, "txt:293b")

    for m in S.BILL_TO.finditer(text):
        if not _own_name_near(text, m.end()):
            add("invoice-issued", 0.25, "txt:billto-foreign")
        else:
            add("invoice-received", 0.40, "txt:billto-own")
        break

    # — invoice-received —
    foreign = _foreign_siret(text)
    if foreign and has_invoice_word:
        add("invoice-received", 0.60, "txt:foreign-siret+invoice")
        fields["foreign_siret"] = foreign
    vendor = _vendor(text, filename)
    if vendor:
        add("invoice-received", 0.65 if has_invoice_word else 0.40, f"vendor:{vendor}")
        fields["counterparty"] = vendor
        fields["expense_category"] = S.VENDOR_CATEGORY.get(vendor, "miscellaneous")
    if S.CORPORATE.search(ftext):
        add("invoice-received", 0.25, "txt:corporate")

    # — the rest: one dominant family each —
    #
    # A filename outweighs body text for these classes. It is authored intent: someone
    # deliberately called the file CONTRAT-APPORT-AFFAIRE, whereas body text is full of
    # incidental vocabulary — a contract discusses "facturation", a quote quotes tax
    # articles, and a signed contract that mentions URSSAF once would otherwise tie
    # with tax-social and fall to review.
    def family(cls: str, pattern, weight_fn: float, weight_txt: float, label: str,
               body_limit: int = 4000) -> None:
        # Both, not either: a filename and the body agreeing is exactly the
        # corroboration that should carry a document over the migrate threshold,
        # while either one alone leaves it for review.
        if pattern.search(fname):
            add(cls, weight_fn, f"fn:{label}")
        if pattern.search(ftext[:body_limit]):
            add(cls, weight_txt, f"txt:{label}")

    if S.QUOTE.search(fname):
        add("quote-issued", 0.75, "fn:devis")
    elif S.QUOTE.search(ftext[:4000]):
        # A quote that also says "facture" is usually an invoice quoting its own terms,
        # so the body-only signal is discounted. The label records which applied — the
        # signals column is the audit trail for why a document landed where it did.
        weak = has_invoice_word
        add("quote-issued", 0.25 if weak else 0.55, "txt:devis-weak" if weak else "txt:devis")

    family("contract", S.CONTRACT, 0.65, 0.50, "contract")
    family("tax-social", S.TAX_SOCIAL, 0.65, 0.50, "tax-social", body_limit=6000)
    family("bank", S.BANK, 0.65, 0.50, "bank")
    family("company-legal", S.COMPANY_LEGAL, 0.65, 0.50, "company-legal")
    family("payroll-employment", S.PAYROLL, 0.65, 0.60, "payroll")
    family("client-material", S.CLIENT_MATERIAL, 0.55, 0.35, "client-material", body_limit=3000)

    client = _client(text, filename)
    if client:
        fields.setdefault("counterparty", client)
        hits.append(f"client:{client}")

    m_dev = S.DEV_SERIES.search(stem)
    if m_dev and "quote-issued" in evidence:
        fields.setdefault("doc_id", m_dev.group(0).upper())

    scores = {cls: _combine(ws) for cls, ws in evidence.items()}
    return scores, hits, fields


# Organism → bucket, for the personal profile. A letter that names its sender is
# strong evidence of where it files even when the body is thin (a one-page CAF
# notification often says little more than "CAF" and an amount). Only organisms
# with one obvious bucket are listed; URSSAF is business paperwork by definition.
_ORG_BUCKET: dict[str, str] = {
    "caf": "logement", "action-logement": "logement",
    "cpam": "sante", "msa": "sante",
    "dgfip": "impots",
    "france-travail": "emploi", "carsat": "emploi",
    "prefecture": "sejour", "ofii": "sejour",
    "ants": "identite",
    "antai": "vehicule",
    "edf": "energie-telecom", "engie": "energie-telecom", "totalenergies": "energie-telecom",
    "bouygues": "energie-telecom", "sfr": "energie-telecom", "orange": "energie-telecom",
    "free": "energie-telecom",
    "lcl": "banque", "boursorama": "banque", "revolut": "banque", "n26": "banque",
    "credit-agricole": "banque", "societe-generale": "banque", "bnp": "banque",
    "caisse-epargne": "banque", "la-poste": "banque",
    "maif": "assurances", "macif": "assurances", "axa": "assurances", "allianz": "assurances",
    "matmut": "assurances", "gmf": "assurances", "maaf": "assurances",
}

# Vetoes that hold under BOTH profiles: another person's document and a live
# credential are never filed anywhere, whoever the space belongs to.
_ALWAYS_VETO = frozenset({"thirdparty", "secret"})


def _classify_personal(
    text: str,
    filename: str,
    channels: list[tuple[str, list[str]]] | None,
) -> PaperClass:
    """The personal profile: file the person's mail, hand the company's paperwork off."""
    fname = fold(filename)
    ftext = fold(text)
    hay = fname + "\n" + ftext[:8000]
    doc_date = _parse_date(text, filename)

    for subclass, pattern in S.VETO:
        if subclass in _ALWAYS_VETO and pattern.search(hay):
            return PaperClass(
                doc_class="personal-handoff", confidence=0.95, signals=[f"veto:{subclass}"],
                veto_subclass=subclass, doc_date=doc_date,
            )

    # The inverse of the business veto: the company's paperwork does not live here.
    if S.BUSINESS_STRICT.search(hay):
        return PaperClass(
            doc_class="business-handoff", confidence=0.90, signals=["business:strict"],
            veto_subclass="business", doc_date=doc_date,
        )

    evidence: dict[str, list[float]] = {}
    hits: list[str] = []

    def add(bucket: str, weight: float, label: str) -> None:
        evidence.setdefault(bucket, []).append(weight)
        hits.append(label)

    for bucket, pattern in S.PERSONAL_FAMILIES:
        if pattern.search(fname):
            add(bucket, 0.65, f"fn:{bucket}")
        if pattern.search(ftext[:6000]):
            add(bucket, 0.50, f"txt:{bucket}")

    # 0.45, not less: a letter that names its sender in the head is the single
    # strongest fact about where it files, and text (0.50) + sender must clear
    # MIGRATE_AT on its own — a photographed letter has no helpful filename.
    org_slug, org_label = C.find_emetteur(text, filename)
    org_bucket = _ORG_BUCKET.get(org_slug, "")
    if org_bucket:
        add(org_bucket, 0.45, f"org:{org_slug}")

    scores = {b: _combine(ws) for b, ws in evidence.items()}
    if not scores:
        letter = C.read_courrier(text, filename, doc_date=doc_date, channels=channels)
        return PaperClass(
            doc_class="unknown", confidence=0.0, signals=hits, doc_date=doc_date,
            counterparty=org_label, emetteur=org_slug, echeance=letter.echeance,
            echeance_source=letter.echeance_source, reference=letter.reference,
            action_requise=letter.action, channel=letter.channel, objet=letter.objet,
        )

    # Stable tie-break: PERSONAL_FAMILIES order (most specific first).
    order = {b: i for i, (b, _) in enumerate(S.PERSONAL_FAMILIES)}
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], order.get(kv[0], 99)))
    bucket, top_score = ranked[0]
    confidence = round(min(top_score, 0.99), 2)
    if len(ranked) > 1 and (top_score - ranked[1][1]) < TIE_WINDOW:
        confidence = min(confidence, REVIEW_AT + 0.1)
        hits.append(f"tie:{ranked[1][0]}")

    letter = C.read_courrier(text, filename, doc_date=doc_date, bucket=bucket, channels=channels)
    return PaperClass(
        doc_class=f"courrier-{bucket}",
        confidence=confidence,
        signals=hits,
        doc_date=doc_date,
        counterparty=letter.emetteur_label,
        bucket=bucket,
        emetteur=letter.emetteur,
        echeance=letter.echeance,
        echeance_source=letter.echeance_source,
        reference=letter.reference,
        action_requise=letter.action,
        channel=letter.channel,
        objet=letter.objet,
    )


def classify(
    text: str,
    filename: str,
    *,
    profile: str = "business",
    channels: list[tuple[str, list[str]]] | None = None,
) -> PaperClass:
    """Classify one document from its extracted text and its filename.

    ``profile`` selects the side of the desk (see ``profiles.py``); ``channels`` is
    the space's routes.yaml taxonomy, used only by the personal profile.
    """
    if profile == "personal":
        return _classify_personal(text, filename, channels)

    veto = check_veto(text, filename)
    if veto:
        # Two decisive signals disagreeing is not something to resolve by rule. A
        # filename carrying the issued-invoice series says "company document"; a veto
        # says "not the company's". Sending it to the handoff manifest would quietly
        # drop what is named as an invoice out of the business archive, so it goes to
        # a person instead — flagged, with both readings recorded.
        if S.DECISIVE_BUSINESS_FILENAME.search(filename):
            return PaperClass(
                doc_class="conflict",
                confidence=0.50,
                signals=[f"veto:{veto}", "conflict:business-filename"],
                doc_date=_parse_date(text, filename),
            )
        return PaperClass(
            doc_class="personal-handoff",
            confidence=0.95,
            signals=[f"veto:{veto}"],
            veto_subclass=veto,
            doc_date=_parse_date(text, filename),
        )

    scores, hits, fields = _score(text, filename)

    # The series suffix in `INV-000017-21` fixes the fiscal year beyond argument.
    prefer_year: int | None = None
    m_id = S.INV_SERIES.search(fields.get("doc_id", ""))
    if m_id:
        prefer_year = 2000 + int(m_id.group(2))
    doc_date = _parse_date(text, filename, prefer_year=prefer_year)

    if not scores:
        return PaperClass(doc_class="unknown", confidence=0.0, signals=hits, doc_date=doc_date)

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top_class, top_score = ranked[0]
    confidence = round(min(top_score, 0.99), 2)

    # A near-tie is a genuine ambiguity, not a winner. Send it to a human.
    if len(ranked) > 1 and (top_score - ranked[1][1]) < TIE_WINDOW:
        confidence = min(confidence, REVIEW_AT + 0.1)
        hits.append(f"tie:{ranked[1][0]}")

    # Only a received invoice or an expense has a *vendor* as its counterparty.
    # Everywhere else the counterparty is a client, and letting a vendor token stand
    # in filed `clients/<name>/` directories named after suppliers — a payments audit
    # mentioning Google became `clients/google/`.
    if top_class in ("invoice-received", "bank"):
        counterparty = fields.get("counterparty", "")
    else:
        counterparty = _client(text, filename)

    return PaperClass(
        doc_class=top_class,
        confidence=confidence,
        signals=hits,
        doc_id=fields.get("doc_id", ""),
        doc_date=doc_date,
        counterparty=counterparty,
        expense_category=fields.get("expense_category", ""),
        amount=_amount(text) if top_class.startswith("invoice") else "",
    )


def decision_for(pc: PaperClass) -> str:
    """Map a classification to its plan decision."""
    if pc.is_handoff:
        return "handoff"
    if pc.confidence >= MIGRATE_AT:
        return "migrate"
    return "review"
