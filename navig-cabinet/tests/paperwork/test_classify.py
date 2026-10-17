"""Classification: issued vs received, and the personal-document veto.

The text fixtures are abbreviated from the operator's real documents — enough of each
to carry the signals that decide it, and nothing identifying beyond what the signal
itself requires.
"""

from __future__ import annotations

from navig_cabinet.paperwork.classify import MIGRATE_AT, classify, decision_for

ISSUED_2025 = """
CYBESIS STUDIOS
SIRET : 753 204 742 00047
FACTURE INV-000022-25
Bill To: CYBERAIGEN SAS
Total HT 4 000,00 EUR
TVA non applicable, article 293 B du CGI
"""

ISSUED_2021 = """
Cybesis Studios — SIRET 75320474200021
FACTURE INV-000017-21
TVA non applicable, article 293B du CGI
"""

RECEIVED_VENDOR = """
SARL NATURAPI
SIRET : 34428220700025 — R.C.S. Montpellier
au capital de 10 000 EUR
FACTURE n 2023-114
Adresse de facturation : Cybesis Studios, Montpellier
TVA 20% : 240,00 EUR
"""

ADMIN_LETTER = """
Conformement a la loi informatique et libertes, vous disposez d un droit d acces.
Objet : votre demande Monsieur. Notre reference concerne le SIREN 753 204 742.
"""

DENTAL = """
NOTE D HONORAIRES
Les soins a tarifs opposables sont rembourses par votre CPAM.
Chirurgien-dentiste — tiers payant
"""

PAYSLIP = """
##BULLETIN## de paie
Convention collective : 00001
N securite sociale 186029912319376
Net imposable 1 842,00
"""

URSSAF = """
URSSAF Languedoc-Roussillon
Attestation d affiliation — auto-entrepreneur
N securite sociale 186029912319376
Chiffre d affaires declare
"""


def test_inv_series_filename_alone_classifies_issued():
    """Two real issued invoices are image-only scans that extract to zero text."""
    pc = classify("", "INV-000018-23-CYBESIS-STUDIOS.pdf")
    assert pc.doc_class == "invoice-issued"
    assert pc.doc_id == "INV-000018-23"


def test_issued_is_detected_by_siren_not_by_the_configured_siret():
    """The config's SIRET ends 00021; invoices since 2025 print 00047."""
    pc = classify(ISSUED_2025, "scan-0042.pdf")
    assert pc.doc_class == "invoice-issued"


def test_the_2021_establishment_siret_also_classifies_issued():
    pc = classify(ISSUED_2021, "old-invoice.pdf")
    assert pc.doc_class == "invoice-issued"


def test_issued_invoice_reaches_migrate_confidence():
    assert decision_for(classify(ISSUED_2025, "INV-000022-25.pdf")) == "migrate"


def test_a_vendor_invoice_with_a_foreign_siret_is_received():
    pc = classify(RECEIVED_VENDOR, "facture (1).pdf")
    assert pc.doc_class == "invoice-received"


def test_a_known_vendor_maps_to_an_existing_expense_category():
    pc = classify("Bouygues Telecom facture du mois", "Bouyguestelecom_Facture_20230720.pdf")
    assert pc.doc_class == "invoice-received"
    assert pc.expense_category == "telecommunications"


def test_siren_in_an_administrative_letter_is_not_an_invoice():
    """SIREN alone means "about our company", not "is an invoice"."""
    assert classify(ADMIN_LETTER, "document (2).pdf").doc_class != "invoice-issued"


def test_note_dhonoraires_named_facture_is_handoff():
    """The highest-value catch: a dental bill named *facture*, filed under invoices."""
    pc = classify(DENTAL, "facture-mizerny-sanmartin-serg-20231214-1202.pdf")
    assert pc.doc_class == "personal-handoff"
    assert pc.veto_subclass == "health"


def test_personal_veto_beats_a_high_scoring_invoice():
    """Invoice-shaped content cannot buy a medical document a place in finance/."""
    pc = classify(DENTAL + ISSUED_2025, "facture-du-cabinet-dentaire.pdf")
    assert pc.doc_class == "personal-handoff"


def test_a_veto_against_an_invoice_series_filename_is_a_conflict_not_a_verdict():
    """Neither answer gets applied silently: a person decides."""
    pc = classify(DENTAL + ISSUED_2025, "INV-000099-26.pdf")
    assert pc.doc_class == "conflict"
    assert decision_for(pc) == "review"


def test_an_id_card_is_vetoed_on_its_filename_when_it_has_no_text():
    """Scanned identity documents extract to zero characters."""
    pc = classify("", "Carte_Identite.pdf")
    assert pc.doc_class == "personal-handoff"
    assert pc.veto_subclass == "identity"


def test_a_third_party_document_is_handoff():
    assert classify("Commande pour Elvira Siadova", "amazon1.pdf").veto_subclass == "thirdparty"


def test_recovery_codes_are_vetoed_as_secrets():
    pc = classify("Your backup codes for two-factor login", "devclined-2fa.txt")
    assert pc.veto_subclass == "secret"


def test_a_payslip_carrying_a_nir_is_still_a_business_document():
    """A NIR is not a personal-vs-business discriminator for a micro-entrepreneur.

    It appears on payslips, URSSAF attestations and company filings alike; vetoing on
    it swept three whole classes of business paperwork into the handoff manifest.
    """
    pc = classify(PAYSLIP, "MIZERNY_SANMARTIN_SERGUEI_02_2022.pdf")
    assert pc.doc_class == "payroll-employment"


def test_a_urssaf_attestation_carrying_a_nir_is_still_business():
    pc = classify(URSSAF, "urssaf-attestation-affiliation-20231202.pdf")
    assert pc.doc_class == "tax-social"


def test_mutuellement_in_a_contract_is_not_a_health_document():
    """`mutuelle` without a word boundary matches the adverb `mutuellement`."""
    text = "Les parties s engagent a s informer mutuellement de toute difficulte."
    pc = classify(text, "CONTRAT-APPORT-AFFAIRE.pdf")
    assert pc.doc_class == "contract"


def test_a_filename_outweighs_incidental_body_vocabulary():
    """A signed contract that mentions URSSAF once must not tie with tax-social."""
    text = "Contrat de prestation. Le prestataire est affilie a l URSSAF."
    assert classify(text, "CONTRAT SERGEY SEPTEMBRE 21-signe.pdf").doc_class == "contract"


def test_a_quote_quoting_its_own_payment_terms_is_not_an_invoice():
    text = "Devis. Conditions de facturation : 30 jours. TVA non applicable article 293 B."
    assert classify(text, "DEVIS-PARCELEO-2026-001.pdf").doc_class == "quote-issued"


def test_a_tie_between_two_classes_lands_in_review_rather_than_being_guessed():
    """The real case: a Guichet Unique filing reads as both tax and company-legal."""
    text = "Synthese de depot au guichet unique. Declaration de CA et cotisation URSSAF."
    pc = classify(text, "88532b0b-421d-4146-839e-bcfd7cdd1a65.pdf")
    assert pc.confidence < MIGRATE_AT
    assert decision_for(pc) == "review"
    assert any(s.startswith("tie:") for s in pc.signals)


def test_corroborated_evidence_clears_the_migrate_threshold():
    """A filename and body that agree is what migration confidence is built on."""
    pc = classify("Attestation d affiliation URSSAF, chiffre d affaires declare",
                  "urssaf-attestation-affiliation-20231202.pdf")
    assert pc.confidence >= MIGRATE_AT
    assert decision_for(pc) == "migrate"


def test_a_single_weak_body_signal_does_not_clear_the_threshold():
    pc = classify("Le present document mentionne une cotisation.", "document (2).pdf")
    assert pc.confidence < MIGRATE_AT


def test_confidence_never_exceeds_certainty_however_much_evidence_stacks():
    """Additive scoring let four weak signals sum past 1.0; noisy-OR cannot."""
    pc = classify(ISSUED_2025 + ISSUED_2021 + RECEIVED_VENDOR, "INV-000022-25.pdf")
    assert pc.confidence <= 0.99


def test_an_unreadable_unknown_document_is_never_migrated():
    assert decision_for(classify("", "20250429185516.pdf")) == "review"
