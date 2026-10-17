"""Every heuristic this plugin uses, as data. No logic, no I/O.

Kept in one module on purpose: the classifier decides where a person's tax and
invoice documents live, so the rules have to be auditable in one screen by someone
who is not going to read ``classify.py``. Anything that inspects a document's text
or filename takes its pattern from here.

Two rules govern edits:

* **A veto is not a score.** ``VETO`` decides *ownership* — is this the company's
  document at all? — and runs before any scoring. It exists because
  ``facture-mizerny-sanmartin-serg-20231214-1202.pdf``, named *facture* and filed
  under *Factures-Invoices*, is a dental fee note. Every scoring signal in this file
  would put it in ``finance/invoices/``.
* **Identity is a SIREN, not a SIRET.** A French business keeps one 9-digit SIREN and
  gets a fresh 14-digit SIRET per établissement. This operator's invoices print
  ``75320474200021`` (2021) and ``75320474200047`` (2025+), so anchoring on either
  literal classifies a fraction of the corpus.
"""

from __future__ import annotations

import re

from navig_sdk.host import command_name  # noqa: E402

# The command a user types here: `navig cabinet` inside navig, `navig-cabinet` on its own.
CMD = command_name("cabinet", standalone="navig-cabinet")

# ── Identity ────────────────────────────────────────────────────────────────
# Separators seen in real invoice headers: none, space, NBSP, dot, hyphen.
_SEP = r"[\s.\- ]?"
SIREN = re.compile(r"753" + _SEP + r"204" + _SEP + r"742")

# Both établissements. Presence is a bonus signal; absence proves nothing.
OWN_SIRET = frozenset({"75320474200021", "75320474200047"})

# NOT "sanmartin": the corpus contains a third party, "Elvira Sanmartin" / "Elvira
# Siadova", on Amazon orders and a payment receipt. A shared surname is not identity.
OWN_NAMES = ("cybesis", "mizerny")

# A SIRET/SIREN *labelled as such*, used to spot a counterparty's number. The capture
# is normalised and compared against SIREN before it counts as foreign.
FOREIGN_SIRET = re.compile(r"(?:SIRET|SIREN)\s*:?\s*((?:\d[\s. ]?){9,14})", re.I)

# ── The veto ────────────────────────────────────────────────────────────────
# Ordered most-specific first; the first hit names the subclass, which picks the
# handoff destination. Matched against accent-folded lowercased text AND filename.
#
# These patterns key on document TYPE, never on the presence of personal data — a
# distinction learned the hard way. A NIR regex was tried here and matched the
# operator's own social-security number ``186029912319376`` on his URSSAF affiliation
# attestation, his payslips and his Guichet Unique filing: all unambiguously *business*
# documents. For a micro-entrepreneur, personal identifiers run through the whole
# corpus, so "contains a NIR" carries no signal. Only "is a prescription", "is an ID
# card" does.
VETO: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        # Decisive: phrases that only ever appear on an actual medical document.
        "health",
        re.compile(
            r"note d.honoraires|tarifs opposables|feuille de soins|"
            r"\bposologie\b|par voie orale|remboursement de soins|"
            r"\bcpam\b|\bameli\b|tiers payant|chirurgien.dentiste|"
            r"medecin traitant|carte vitale|releve de prestations",
            re.I,
        ),
    ),
    (
        # Medical *vocabulary*, which a business document can legitimately contain: a
        # real issued invoice in this corpus bills for "intégration mutuelles" — work
        # done for an insurance client. Overridable by a business anchor, unlike the
        # decisive phrases above.
        #
        # \b matters on `mutuelle` for a second reason: without it, it matches
        # `mutuellement` ("s'informer mutuellement") in ordinary commercial contracts.
        "health-domain",
        re.compile(
            r"\bmutuelles?\b|\bdentiste\b|radiologie|\bpharmacie\b|"
            r"laboratoire d.analyses|\bmedecin\b",
            re.I,
        ),
    ),
    (
        "identity",
        re.compile(
            r"carte nationale d.identite|\bpasseport\b|acte de naissance|"
            r"titre de sejour|certificat de naissance|"
            r"свидетельство о рождении|"
            r"republique francaise.{0,40}carte d.identite",
            re.I,
        ),
    ),
    (
        "benefits",
        re.compile(
            r"\bcaf\b|\brsa\b|prime d.activite|"
            r"pole emploi|france travail|demandeur d.emploi|"
            r"actualisation mensuelle|allocation de retour a l.emploi",
            re.I,
        ),
    ),
    (
        "housing",
        re.compile(
            r"reference locataire|quittance de loyer|contrat de bail|"
            r"\bapl\b|attestation d.hebergement|taxe d.habitation",
            re.I,
        ),
    ),
    (
        "thirdparty",
        re.compile(r"elvira\s+(?:siadova|sanmartin)|siadova|polishchuk|yelyzaveta", re.I),
    ),
    (
        "secret",
        re.compile(
            r"recovery cod|code de recuperation|backup cod|"
            r"\b2fa\b|authenticator|one.time password|seed phrase",
            re.I,
        ),
    ),
)

# Filenames that settle ownership on their own. Needed because scanned identity
# documents are image-only: `Carte_Identite.pdf` extracts to zero characters, so the
# text patterns above never see it.
VETO_FILENAME: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("identity", re.compile(r"carte.?(?:nationale.?d.?)?identite|carte.?vitale|passeport|"
                            r"acte.?de.?naissance|titre.?de.?sejour|\bcni\b", re.I)),
    ("health", re.compile(r"mutuelle|ordonnance|tiers.?payant|honoraires|"
                          r"attestation.?de.?droits|carte.?vitale", re.I)),
    ("housing", re.compile(r"hebergement|quittance|loyer", re.I)),
    ("benefits", re.compile(r"pole.?emploi|france.?travail|attestation.?paiement", re.I)),
)

# Subclasses that a strong business anchor may override. A document that is plainly
# URSSAF/tax/company paperwork is business even when it carries personal identifiers;
# `health`, `thirdparty` and `secret` are never overridable, because a medical record
# or a live credential is not the company's regardless of what else is on the page.
SOFT_VETO = frozenset({"identity", "benefits", "housing", "health-domain"})

# Filename evidence strong enough to contradict a veto outright. When one of these is
# present and a veto still fires, the two decisive signals disagree and the document
# goes to a human rather than being silently removed from the company's space.
#
# The case that forced this: `INV-000021-23.pdf` carries the issued-invoice series in
# its name, but its OCR'd content is a letter about failed CAF logins. One of those
# facts is wrong and no rule can tell which — but sending it to the handoff manifest
# would quietly drop what is named as an invoice out of the business archive entirely.
DECISIVE_BUSINESS_FILENAME = re.compile(r"\bINV-\d{6}-\d{2}\b", re.I)

# Where each veto subclass belongs. A ``None`` space is not a space's problem at all.
#
# identity / health go to the **encrypted cabinet** (`navig cabinet`, same plugin): an ID
# scan or a medical record is a document a person *keeps*, and a plaintext copy in a space
# tree is one `git add` away from a commit. The health veto cannot tell a CPAM attestation
# from a prescription, so both are encrypted — the safe side of that ambiguity.
# benefits / housing still go to **company-paperwork**: those letters are dossiers a space
# works with (deadlines, replies), filed under its `personal/logement/` tree.
#
# `business` is the reverse direction: a URSSAF notice or an issued invoice found while
# scanning a *personal* space belongs to the company space.
HANDOFF_MAP: dict[str, dict[str, str | None]] = {
    "health": {"space": None, "path": None, "tool": f"{CMD}"},
    "health-domain": {"space": None, "path": None, "tool": f"{CMD}"},
    "identity": {"space": None, "path": None, "tool": f"{CMD}"},
    "benefits": {"space": "company-paperwork", "path": "personal/logement/", "tool": None},
    "housing": {"space": "company-paperwork", "path": "personal/logement/", "tool": None},
    "business": {"space": "company", "path": "finance/", "tool": None},
    "thirdparty": {"space": None, "path": None, "tool": None},
    # A recovery code written into any plaintext markdown tree is the wrong answer
    # regardless of which tree, so this routes to a tool rather than a space.
    "secret": {"space": None, "path": None, "tool": "navig vault"},
}

# ── Document identifiers ────────────────────────────────────────────────────
# The issued series, verbatim as printed. Uppercase is preserved everywhere: it is a
# legal identifier under the French sequential-numbering rule and must stay greppable.
INV_SERIES = re.compile(r"\bINV-(\d{6})-(\d{2})\b")
INV_SERIES_STEM = re.compile(r"^INV-(\d{6})-(\d{2})$", re.I)
DEV_SERIES = re.compile(r"\bDEV(?:IS)?[-_ ]?(\d{2,6})(?:-(\d{2,4}))?\b", re.I)

# ── Class signals ───────────────────────────────────────────────────────────
# Bounded on purpose: a bare `\bfactur` prefix also matches "facturation", which
# appears in the payment-terms paragraph of every quote and contract, and was enough
# to score DEVIS-PARCELEO-2026-001 as an invoice.
INVOICE_WORD = re.compile(r"\bfactures?\b|\binvoice\b|note de debit", re.I)

# Unambiguous markers that a document is the company's business paperwork. Used only
# to override a SOFT_VETO subclass — see `classify.check_veto`.
BUSINESS_ANCHOR = re.compile(
    r"\burssaf\b|impots?\.gouv|guichet unique|\bsiret\b|\bsiren\b|"
    r"micro.?entrepreneur|auto.?entrepreneur|chiffre d.affaires|"
    r"bulletin de paie|convention collective|declaration de ca|"
    # Deliberately NOT the INV- series. A business filename contradicting a veto is
    # handled by DECISIVE_BUSINESS_FILENAME, which sends the disagreement to a person;
    # listing it here instead would resolve it silently in the business's favour.
    r"tva non applicable",
    re.I,
)

# "TVA non applicable, article 293 B du CGI" — the franchise-en-base mention. A
# VAT-registered vendor states a rate instead, so this separates issued from
# received rather than merely marking a document as French.
TVA_293B = re.compile(r"293\s*-?\s*b\b", re.I)
TVA_EXEMPT = re.compile(r"tva non applicable|franchise en base", re.I)

# A counterparty block. The 200 chars after it are searched for OWN_NAMES: our own
# name as the *addressee* means the document was issued to us, i.e. received.
BILL_TO = re.compile(
    r"bill\s*to|adresse de facturation|factur[ea] a\b|client\s*:|destinataire", re.I
)

# A line inside an address block rather than the party's name: a street, a postcode,
# a country, a registration number. Skipped when reading the addressee, so a
# counterparty comes out as "FETCH NETWORK" and not "27 Place Aguesseau".
ADDRESS_LINE = re.compile(
    r"^\s*(?:\d+[\s,]|\d{5}\b|b\.?p\.?\s|rue\b|avenue\b|av\.|bd\b|boulevard\b|"
    r"place\b|chemin\b|impasse\b|route\b|allee\b|cedex\b|france\b|"
    r"rcs\s*:|siret\s*:|siren\s*:|tel\b|email\b|www\.)",
    re.I,
)

# Corporate-registration boilerplate a micro-entrepreneur never prints.
CORPORATE = re.compile(r"\br\.?c\.?s\.?\b|au capital de|siege social|\bsarl\b|\bsas\b", re.I)

VENDORS: tuple[str, ...] = (
    "bouygues", "sfr", "orange", "free mobile", "edf", "engie", "total energies",
    "amazon", "aws", "envato", "themeforest", "codecanyon", "cssninja",
    "ovh", "o2switch", "gandi", "namecheap", "cloudflare",
    "google", "microsoft", "adobe", "jetbrains", "github", "openai", "anthropic",
    "iobit", "paypal", "stripe", "wise", "qonto", "revolut", "shine",
    "naturapi", "ikea", "leroy merlin", "fnac", "cdiscount", "lcl", "boursorama",
)

# Vendor token -> expense category, using the 21 categories already declared in the
# space's own .navig/config.yaml. No new taxonomy is invented here.
VENDOR_CATEGORY: dict[str, str] = {
    "bouygues": "telecommunications", "sfr": "telecommunications",
    "orange": "telecommunications", "free mobile": "telecommunications",
    "edf": "miscellaneous", "engie": "miscellaneous", "total energies": "miscellaneous",
    "ovh": "domain-hosting", "o2switch": "domain-hosting", "gandi": "domain-hosting",
    "namecheap": "domain-hosting", "cloudflare": "domain-hosting",
    "envato": "software-licenses", "themeforest": "software-licenses",
    "codecanyon": "software-licenses", "cssninja": "software-licenses",
    "adobe": "software-licenses", "jetbrains": "software-licenses",
    "microsoft": "software-licenses", "iobit": "software-licenses",
    "github": "subscriptions-saas", "google": "subscriptions-saas",
    "openai": "subscriptions-saas", "anthropic": "subscriptions-saas",
    "aws": "subscriptions-saas",
    "amazon": "hardware", "fnac": "hardware", "cdiscount": "hardware",
    "ikea": "office-supplies", "leroy merlin": "office-supplies",
    "paypal": "banking-fees", "stripe": "banking-fees", "wise": "banking-fees",
    "qonto": "banking-fees", "revolut": "banking-fees", "shine": "banking-fees",
    "lcl": "banking-fees", "boursorama": "banking-fees",
    "naturapi": "miscellaneous",
}

QUOTE = re.compile(r"\bdevis\b|quotation|proposition commerciale|\boffres?\b|estimate", re.I)

CONTRACT = re.compile(
    r"\bcontrat\b|\bconvention\b|pacte d.associes|conditions generales|"
    r"\bcgu\b|\bcgv\b|\bnda\b|non.disclosure|promesse d.embauche|"
    r"bon de commande|lettre de mission|charte d.utilisation|apport d.affaire",
    re.I,
)

TAX_SOCIAL = re.compile(
    r"\burssaf\b|impots?\.gouv|avis d.impot|declaration de revenus|"
    r"\bacre\b|\bcfe\b|cotisation|attestation de vigilance|attestation d.affiliation|"
    r"radiation|declaration de ca|chiffre d.affaires|prelevement a la source|"
    r"\b2042\b|telereglement|avis de situation declarative",
    re.I,
)

BANK = re.compile(
    r"releve de compte|extrait de compte|releve d.identite bancaire|"
    r"\brib\b|\biban\b.{0,80}\bbic\b|solde crediteur",
    re.I,
)

COMPANY_LEGAL = re.compile(
    r"guichet unique|synthese de depot|\binpi\b|\binsee\b|k-?bis|"
    r"avis de situation au repertoire|\bsirene\b|greffe du tribunal",
    re.I,
)

PAYROLL = re.compile(
    r"##bulletin##|bulletin de paie|bulletin de salaire|net imposable|"
    r"salaire brut|cumul imposable",
    re.I,
)

CLIENT_MATERIAL = re.compile(
    r"\bbrief\b|\bbriefing\b|refonte|referencement|cahier des charges|"
    r"\bmission\b|\bbilan\b|\baudit\b|checklist|wireframe|maquette",
    re.I,
)

# Known clients, discovered in the corpus. Used to slug ``clients/<name>/``.
CLIENTS: tuple[str, ...] = (
    "iaka", "cyberaigen", "parceleo", "trianglepacket",
    "paypersafe", "clvmstv", "chii00ta", "pomzed", "ysyone",
)

# ── Dates ───────────────────────────────────────────────────────────────────
# (pattern, field order) — French documents are day-first, ISO ones year-first, and
# guessing wrong silently files a document in the wrong fiscal year.
#
# The guards are `(?<!\d)` / `(?!\d)`, not `\b`: a word boundary does not exist between
# a letter and a digit, so `\b(20\d{2})` never matches `invoice2022-04-25.pdf` — a real
# filename that came out undated. What must be excluded is a longer digit run, not an
# adjacent letter.
_ND = r"(?<!\d)"
_DN = r"(?!\d)"
DATE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(_ND + r"(20\d{2})[-/.](\d{2})[-/.](\d{2})" + _DN), "ymd"),
    (re.compile(_ND + r"(\d{2})[-/.](\d{2})[-/.](20\d{2})" + _DN), "dmy"),
    (re.compile(_ND + r"(20\d{2})(\d{2})(\d{2})" + _DN), "ymd"),
    (re.compile(_ND + r"(\d{2})[_\s](\d{2})[_\s](20\d{2})" + _DN), "dmy"),
)

# A date label, so "Invoice Date: 31/08/2026" outranks an incidental date elsewhere on
# the page. Searching label-first is what stops a 2021 invoice being filed under 2008.
#
# Every alternative is word-bounded. An unbounded `le` matched inside "Té-le-com" and
# handed back the order date instead of the invoice date on a Bouygues bill.
DATE_LABEL = re.compile(
    r"\b(?:invoice\s+date|date\s+de\s+facture|date\s+d.(?:emission|edition)|"
    r"emise?\s+le|fait\s+le|date)\b\s*:?\s*",
    re.I,
)

# Documents older than the business itself, or dated in the future, are parse errors
# rather than facts. Cybesis was founded in 2009.
MIN_YEAR = 2009

FR_MONTHS: dict[str, int] = {
    "janvier": 1, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11,
    "decembre": 12,
}
FR_DATE = re.compile(r"\b(\d{1,2})\s+(" + "|".join(FR_MONTHS) + r")\s+(20\d{2})\b", re.I)

# Matched against text with ALL whitespace removed — see `classify._amount`. A PDF's
# text layer breaks words wherever the line broke, so a real invoice extracts as
# "Sub T\notal €2000T\notal €2000" and the literal string "total" never appears.
# Stripping whitespace repairs that, and collapses a French thousands space
# ("4 000,00" -> "4000,00") for free.
#
# Ordered MOST SPECIFIC FIRST, and the order is the whole point. Taking the last
# figure on the page reads a deposit invoice's note — "facture d'acompte sur le
# montant total de 3700" — as the invoice's own amount, and it is not: that invoice
# is for 1.110,00. Ask for `total ttc` before `total`, and a labelled figure before
# a bare currency one.
#
# (?<![a-z-]) matters: without it, `total` matches inside `subtotal`.
_AMOUNT_VALUE = r"[^\d]{0,6}(\d[\d.,]*\d|\d)"
AMOUNT_LABELS = (
    ("total ttc", re.compile(r"(?<![a-z-])totalttc" + _AMOUNT_VALUE, re.I)),
    ("total ht", re.compile(r"(?<![a-z-])totalht" + _AMOUNT_VALUE, re.I)),
    ("net a payer", re.compile(r"(?<![a-z-])net+apayer" + _AMOUNT_VALUE, re.I)),
    ("total", re.compile(r"(?<![a-z-])total(?!de)" + _AMOUNT_VALUE, re.I)),
)
# Last resort for an invoice that labels nothing: a number against a currency marker.
AMOUNT_CURRENCY = re.compile(
    r"(?:eur|€)(\d[\d.,]*\d|\d)|(\d[\d.,]*\d|\d)(?:eur|€)", re.I
)


# ══════════════════════════════════════════════════════════════════════════════
# Personal profile — filing a person's administrative mail ("le courrier")
# ══════════════════════════════════════════════════════════════════════════════
# Used only when the destination space runs the `personal` profile (its
# `.navig/config.yaml` says `paperwork.profile: personal`, or `--profile personal`).
# There the veto logic is inverted: the person's documents are the ones being FILED,
# and it is the company's paperwork that gets handed off. All patterns match folded
# (lowercase, accent-stripped) text, like everything else in this file.

# Unambiguously the company's paperwork, even when found in the personal inbox. Kept
# strict on purpose: `\bsiret\b` alone appears in the footer of every French vendor
# invoice, so it cannot decide ownership here the way BUSINESS_ANCHOR does above.
BUSINESS_STRICT = re.compile(
    r"753" + _SEP + r"204" + _SEP + r"742|\burssaf\b|micro.?entrepreneur|auto.?entrepreneur|"
    r"chiffre d.affaires|guichet unique|\bcfe\b|tva non applicable|293\s*-?\s*b\b|"
    r"attestation de vigilance|declaration de ca|cotisations? sociales?.{0,40}independants?|"
    r"\bINV-\d{6}-\d{2}\b|\bDEV(?:IS)?[-_ ]?\d{2,6}\b|cybesis",
    re.I,
)

# One family per `personal/<bucket>/` folder of the paperwork space. Weights follow
# the business classes: a filename is authored intent (0.65), body text is incidental
# vocabulary (0.50); both agreeing clears MIGRATE_AT, one alone lands in review.
# Ordered most-specific first — it is also the tie-break order.
PERSONAL_FAMILIES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("sejour", re.compile(
        r"titre de sejour|carte de sejour|recepisse|\bprefecture\b|sous.prefecture|\bofii\b|"
        r"\banef\b|autorisation provisoire de sejour|demande de titre|\bvisa\b.{0,20}(?:long|sejour)",
        re.I)),
    ("identite", re.compile(
        r"carte nationale d.identite|\bcni\b|\bpasseport\b|acte de naissance|permis de conduire|"
        r"\bants\b|certificat de nationalite|livret de famille|carte d.identite",
        re.I)),
    ("sante", re.compile(
        r"\bcpam\b|\bameli\b|carte vitale|\bmutuelle\b|tiers payant|attestation de droits|"
        r"feuille de soins|remboursement de soins|releve de prestations|complementaire sante|"
        r"assurance maladie|securite sociale|caisse primaire|\bcss\b",
        re.I)),
    ("logement", re.compile(
        r"quittance|\bloyer\b|contrat de bail|\bbail\b|\bapl\b|\bcaf\b|allocations? familiales|"
        r"allocation logement|taxe d.habitation|etat des lieux|\bbailleur\b|\bsyndic\b|"
        r"charges locatives|depot de garantie|\bpreavis\b|attestation d.hebergement",
        re.I)),
    ("energie-telecom", re.compile(
        r"\bedf\b|\bengie\b|total ?energies|\bbouygues\b|\bsfr\b|free ?mobile|freebox|\biliad\b|"
        r"livebox|orange (?:france|sa|telecom)|\belectricite\b|\bgaz\b|\bfibre\b|forfait mobile|"
        r"\bcompteur\b|\blinky\b|releve de consommation|\bkwh\b",
        re.I)),
    ("banque", re.compile(
        r"releve de compte|extrait de compte|\brib\b|\biban\b|carte bancaire|frais bancaires|"
        r"\blcl\b|boursorama|\brevolut\b|\bn26\b|credit agricole|societe generale|\bbnp\b|"
        r"caisse d.epargne|banque postale|\bqonto\b|offre de pret|tableau d.amortissement",
        re.I)),
    ("assurances", re.compile(
        r"assurance habitation|responsabilite civile|contrat d.assurance|\bassureur\b|\bsinistre\b|"
        r"\bmaif\b|\bmacif\b|\baxa\b|\ballianz\b|\bmatmut\b|\bgmf\b|\bmaaf\b|attestation d.assurance|"
        r"avis d.echeance|multirisque",
        re.I)),
    ("vehicule", re.compile(
        r"carte grise|certificat d.immatriculation|controle technique|assurance auto|\bantai\b|"
        r"avis de contravention|\bamende forfaitaire|retrait de points|\bimmatriculation\b",
        re.I)),
    ("impots", re.compile(
        r"avis d.impot|avis d.imposition|declaration de revenus|prelevement a la source|"
        r"taxe fonciere|\bdgfip\b|impots?\.gouv|finances publiques|\b2042\b|\b2044\b|"
        r"tresor public|centre des finances|direction generale des finances",
        re.I)),
    ("emploi", re.compile(
        r"pole emploi|france travail|demandeur d.emploi|actualisation mensuelle|"
        r"allocation de retour|attestation employeur|contrat de travail|bulletin de paie|"
        r"bulletin de salaire|fiche de paie|solde de tout compte|certificat de travail",
        re.I)),
    ("abonnements", re.compile(
        r"\babonnement\b|\bnetflix\b|\bspotify\b|salle de sport|\badhesion\b|"
        r"cotisation annuelle|renouvellement automatique|\bresiliation\b",
        re.I)),
)
PERSONAL_BUCKETS: tuple[str, ...] = tuple(b for b, _ in PERSONAL_FAMILIES)

# Who sent the letter. (slug, display name, pattern) — the slug goes into the filename,
# the display name into the ledger and the Telegram line. First match wins, so public
# bodies come before vendors and vendors before banks.
ORGANISMS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("caf", "CAF", re.compile(r"\bcaf\b|caisse d.allocations familiales", re.I)),
    ("cpam", "CPAM", re.compile(r"\bcpam\b|\bameli\b|caisse primaire|assurance maladie", re.I)),
    ("urssaf", "URSSAF", re.compile(r"\burssaf\b", re.I)),
    ("dgfip", "DGFiP", re.compile(r"\bdgfip\b|finances publiques|impots?\.gouv|tresor public|centre des finances", re.I)),
    ("france-travail", "France Travail", re.compile(r"france travail|pole emploi", re.I)),
    ("prefecture", "Préfecture", re.compile(r"\bprefecture\b|sous.prefecture", re.I)),
    ("ofii", "OFII", re.compile(r"\bofii\b", re.I)),
    ("ants", "ANTS", re.compile(r"\bants\b|agence nationale des titres", re.I)),
    ("antai", "ANTAI", re.compile(r"\bantai\b|avis de contravention", re.I)),
    ("carsat", "CARSAT", re.compile(r"\bcarsat\b|assurance retraite", re.I)),
    ("msa", "MSA", re.compile(r"\bmsa\b|mutualite sociale agricole", re.I)),
    ("mairie", "Mairie", re.compile(r"\bmairie\b|hotel de ville", re.I)),
    ("action-logement", "Action Logement", re.compile(r"action logement", re.I)),
    ("la-poste", "La Poste", re.compile(r"\bla poste\b|banque postale", re.I)),
    ("edf", "EDF", re.compile(r"\bedf\b", re.I)),
    ("engie", "Engie", re.compile(r"\bengie\b", re.I)),
    ("totalenergies", "TotalEnergies", re.compile(r"total ?energies", re.I)),
    ("bouygues", "Bouygues Telecom", re.compile(r"\bbouygues\b", re.I)),
    ("sfr", "SFR", re.compile(r"\bsfr\b", re.I)),
    ("orange", "Orange", re.compile(r"orange (?:france|sa|telecom)|livebox", re.I)),
    ("free", "Free", re.compile(r"free ?mobile|freebox|\biliad\b", re.I)),
    ("lcl", "LCL", re.compile(r"\blcl\b|credit lyonnais", re.I)),
    ("boursorama", "Boursorama", re.compile(r"boursorama", re.I)),
    ("revolut", "Revolut", re.compile(r"\brevolut\b", re.I)),
    ("n26", "N26", re.compile(r"\bn26\b", re.I)),
    ("credit-agricole", "Crédit Agricole", re.compile(r"credit agricole", re.I)),
    ("societe-generale", "Société Générale", re.compile(r"societe generale", re.I)),
    ("bnp", "BNP Paribas", re.compile(r"\bbnp\b", re.I)),
    ("caisse-epargne", "Caisse d'Épargne", re.compile(r"caisse d.epargne", re.I)),
    ("maif", "MAIF", re.compile(r"\bmaif\b", re.I)),
    ("macif", "MACIF", re.compile(r"\bmacif\b", re.I)),
    ("axa", "AXA", re.compile(r"\baxa\b", re.I)),
    ("allianz", "Allianz", re.compile(r"\ballianz\b", re.I)),
    ("matmut", "Matmut", re.compile(r"\bmatmut\b", re.I)),
    ("gmf", "GMF", re.compile(r"\bgmf\b", re.I)),
    ("maaf", "MAAF", re.compile(r"\bmaaf\b", re.I)),
)

# A deadline is a date sitting right after one of these. Matched on folded text; the
# 60 characters after the label are handed to the date finder.
DEADLINE_LABEL = re.compile(
    r"(?:avant le|au plus tard le|jusqu.au|date limite(?: de (?:paiement|reponse|depot|retour))?\s*:?|"
    r"echeance(?: de paiement)?\s*:?|a regler avant le|a payer avant le|a retourner avant le|"
    r"a nous retourner avant le|delai de reponse\s*:?|reponse (?:attendue|souhaitee) (?:avant|pour) le|"
    r"vous avez jusqu.au|payable avant le|paiement attendu (?:avant|pour) le|"
    r"vous disposez d.un delai.{0,30}?jusqu.au|prelev(?:e|ement)s?\s+(?:le|prevu le|a partir du)|"
    r"sera preleve le|a compter du|au plus tard)",
    re.I,
)
# A relative deadline: "sous 15 jours", "dans un delai de deux mois", "sous quinzaine".
DEADLINE_RELATIVE = re.compile(
    r"(?:sous|dans un delai de|dans les|sous un delai de|d.ici|dans)\s+"
    r"(\d{1,3}|un|une|deux|trois|quatre|cinq|six|huit|dix|quinze|trente)\s*(jours?|semaines?|mois)\b"
    r"|sous\s+(quinzaine|huitaine)",
    re.I,
)
FR_NUMBER_WORDS: dict[str, int] = {
    "un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "six": 6, "huit": 8,
    "dix": 10, "quinze": 15, "trente": 30,
}

# A case / contract / customer reference. The value is captured raw for the plan; the
# ledger keeps only a short hash of it. The NIR (social-security number) is deliberately
# NOT extracted — it identifies a person, not a dossier, and must never leave the page.
REFERENCE_LABEL = re.compile(
    r"(?:n(?:o|°|um(?:ero)?)?\.?\s*(?:de\s+)?(?:dossier|allocataire|contrat|client|compte|reference|"
    r"sinistre|adherent|assure|police|demande|avis|role)|(?:votre\s+|notre\s+)?reference|ref\.?)\s*:?\s*"
    # A space is allowed only before a digit ("12 345 678"), never before a word —
    # otherwise the capture runs on into the sentence ("ABC-2026-77 merci").
    r"([A-Z0-9][A-Z0-9./-]*(?: \d[A-Z0-9./-]*)*)",
    re.I,
)

# What the letter asks for. First family with a hit wins; none → "info".
ACTIONS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("payer", re.compile(r"a regler|a payer|montant (?:du|a payer|restant)|reglement|paiement|"
                         r"mise en demeure|relance|impaye|penalites? de retard|prelevement", re.I)),
    ("fournir", re.compile(r"justificatifs?|pieces? (?:manquantes?|justificatives?|a fournir)|"
                           r"documents? (?:manquants?|a fournir|complementaires?)|nous (?:transmettre|adresser|retourner)|"
                           r"joindre|merci de (?:nous )?(?:fournir|renvoyer|retourner)", re.I)),
    ("declarer", re.compile(r"declarer|declaration (?:de|des|trimestrielle|annuelle)|actualis|"
                            r"mettre a jour (?:votre|vos) (?:situation|ressources|coordonnees)", re.I)),
    ("repondre", re.compile(r"convocation|rendez.vous|reponse (?:attendue|souhaitee)|confirmer|"
                            r"nous contacter|prendre contact|entretien|se presenter", re.I)),
)
