"""Regressions found by running the tool against a real archive.

Each of these shipped a wrong answer on the first full scan of ~31,000 files.
"""

from __future__ import annotations


from navig_cabinet.paperwork.classify import classify
from navig_cabinet.paperwork.plan import PlanRow, _assign_destinations, iter_candidates


def test_the_invoice_series_year_outranks_a_stray_date_on_the_page():
    """INV-000017-21 was filed under 2008.

    The 2021 invoice carries an unrelated `04/08/2008` in its body. The series suffix
    is authoritative — a document numbered `-21` was issued in 2021 by definition — so
    a stray date elsewhere on the page cannot decide its fiscal year.
    """
    text = "FACTURE # INV-000017-21\nSIRET: 75320474200021\nref 04/08/2008\n"
    pc = classify(text, "INV-000017-21-CYBESIS-STUDIOS.pdf")
    assert pc.doc_date.startswith("2021")


def test_a_date_matching_the_series_year_is_preferred_over_the_first_one_found():
    text = "Ref 15/03/2019. FACTURE INV-000030-26. Invoice Date: 14/04/2026"
    assert classify(text, "INV-000030-26.pdf").doc_date == "2026-04-14"


def test_an_invoice_with_no_usable_date_still_lands_in_the_right_year():
    """Better a bare year from the identifier than a confidently wrong day."""
    pc = classify("FACTURE INV-000033-26 sans date lisible", "INV-000033-26.pdf")
    assert pc.doc_date == "2026"


def test_a_date_immediately_after_a_filename_word_is_still_found():
    """`\\b` does not match between a letter and a digit, so `invoice2022-04-25.pdf`
    came out undated and filed under `received/undated/`."""
    pc = classify("SARL NATURAPI SIRET : 34428220700025 facture", "invoice2022-04-25_14-06-33.pdf")
    assert pc.doc_date == "2022-04-25"


def test_a_labelled_date_outranks_an_incidental_one():
    text = "Commande du 02/01/2023 — Invoice Date: 20/07/2023"
    pc = classify("Bouygues Telecom " + text, "bt.pdf")
    assert pc.doc_date == "2023-07-20"


def test_a_date_before_the_business_existed_is_rejected():
    pc = classify("URSSAF cotisation ref 01/01/1998", "urssaf-truc.pdf")
    assert not pc.doc_date.startswith("1998")


def test_a_disambiguated_destination_stays_a_posix_path():
    """One dest_rel came out as `finance\\invoices\\issued\\2008\\…` on Windows."""
    rows = [
        PlanRow(row_id="a", decision="migrate", doc_class="invoice-issued",
                doc_id="INV-000017-21", doc_date="2021-05-20", src=r"H:\a.pdf",
                sha256="1" * 64, confidence=0.99),
        PlanRow(row_id="b", decision="migrate", doc_class="invoice-issued",
                doc_id="INV-000017-21", doc_date="2021-05-20", src=r"H:\b.pdf",
                sha256="2" * 64, confidence=0.99),
    ]
    _assign_destinations(rows)
    assert all("\\" not in r.dest_rel for r in rows)
    assert rows[0].dest_rel != rows[1].dest_rel


def test_media_files_are_not_candidates_by_default(tmp_path):
    """A downloads folder holds a messaging app's cache; none of it is paperwork."""
    (tmp_path / "invoice.pdf").write_bytes(b"x")
    (tmp_path / "photo.jpg").write_bytes(b"x")
    (tmp_path / "voice.ogg").write_bytes(b"x")
    (tmp_path / "song.mp3").write_bytes(b"x")

    found = {p.name for p in iter_candidates([tmp_path])}
    assert found == {"invoice.pdf"}


def test_all_types_lifts_the_document_filter(tmp_path):
    (tmp_path / "invoice.pdf").write_bytes(b"x")
    (tmp_path / "scan.jpg").write_bytes(b"x")

    found = {p.name for p in iter_candidates([tmp_path], all_types=True)}
    assert found == {"invoice.pdf", "scan.jpg"}


def test_a_dev_series_filename_does_not_crash_the_classifier():
    """`scores` was read before assignment, so any `DEV-<digits>` stem raised
    UnboundLocalError. The probe corpus missed it because `DEVIS-PARCELEO` has no
    digits directly after the prefix."""
    pc = classify("Devis de prestation pour le client", "DEV-2026-001.pdf")
    assert pc.doc_class == "quote-issued"
    assert pc.doc_id.startswith("DEV")


def test_a_business_filename_contradicting_a_veto_goes_to_a_human():
    """`INV-000021-23.pdf` is named as an issued invoice but reads as a CAF letter.

    Silently routing it to the handoff manifest would drop what is named as an invoice
    out of the business archive entirely. Two decisive signals disagreeing is a
    question for a person, not a rule.
    """
    text = "tentatives de connexion echouees a allocataire sur le site caf.fr"
    pc = classify(text, "INV-000021-23.pdf")
    assert pc.doc_class == "conflict"
    assert pc.veto_subclass is None          # so it is not written to the manifest
    assert any("conflict" in s for s in pc.signals)
    from navig_cabinet.paperwork.classify import decision_for
    assert decision_for(pc) == "review"


def test_medical_vocabulary_on_a_real_invoice_is_not_a_medical_document():
    """A genuine issued invoice bills for "intégration mutuelles" — insurance work."""
    text = ("FACTURE INV-000024-25\nSIRET: 75320474200047\n"
            "analyse du systeme OTP et integration mutuelles\n"
            "TVA non applicable, article 293 B du CGI")
    pc = classify(text, "INV-000024-25.pdf")
    assert pc.doc_class == "invoice-issued"


def test_a_decisive_medical_phrase_still_wins_over_a_business_anchor():
    """The override must not reopen the dental-bill hole it was carved around."""
    text = "NOTE D HONORAIRES — soins a tarifs opposables. SIRET 75320474200047"
    pc = classify(text, "facture-du-cabinet.pdf")
    assert pc.doc_class == "personal-handoff"
    assert pc.veto_subclass == "health"


def test_duplicates_are_held_when_the_kept_copy_still_needs_review():
    """INV-000024-25 had three copies bound for quarantine and none for the archive."""
    from navig_cabinet.paperwork.plan import PlanRow, _mark_duplicates

    rows = [
        PlanRow(row_id="k", decision="migrate", doc_class="invoice-issued",
                src=r"H:\x\INV-000024-25.pdf", sha256="a" * 64, size=137000, mtime=1.0),
        PlanRow(row_id="d1", decision="migrate", doc_class="invoice-issued",
                src=r"H:\x\INV-000024-25 (1).pdf", sha256="a" * 64, size=137000, mtime=2.0),
        # a near-duplicate with different content demotes the keeper to review
        PlanRow(row_id="n", decision="migrate", doc_class="invoice-issued",
                src=r"H:\x\INV-000024-25 (3).pdf", sha256="b" * 64, size=86000, mtime=3.0),
    ]
    _mark_duplicates(rows, [r"H:\x"])

    assert not any(r.decision == "trash-dupe" for r in rows), \
        "no copy may be quarantined while the kept copy is unresolved"


def test_a_vendor_never_becomes_a_client_directory():
    """A payments audit mentioning Google was filed as `clients/google/`.

    Only a received invoice has a vendor as its counterparty; everywhere else the
    counterparty names a client, and that name becomes a directory.
    """
    pc = classify("Audit des paiements Google Pay et Stripe pour le projet",
                  "payments-audit.md")
    assert pc.doc_class == "client-material"
    assert pc.counterparty == ""


def test_a_received_invoice_still_keeps_its_vendor():
    pc = classify("Bouygues Telecom facture mensuelle", "Bouyguestelecom_Facture.pdf")
    assert pc.doc_class == "invoice-received"
    assert pc.counterparty == "bouygues"


def test_an_unknown_client_is_read_from_the_addressee_block():
    """A hardcoded client list only ever knows yesterday's clients.

    The most recent invoice in the corpus is addressed to a company the list had
    never heard of, and came back with no counterparty at all.
    """
    text = (
        "Mizerny Sanmartin Serguei\nCybesis Studios\nSIRET: 75320474200047\n"
        "Bill To\nFETCH NETWORK\n27 Place Aguesseau\n34000 Montpellier\n"
        "FACTURE INV-000035-26\n"
    )
    pc = classify(text, "INV-000035-26.pdf")
    assert pc.counterparty == "FETCH NETWORK"


def test_the_addressee_is_the_name_not_the_street():
    text = "Bill To\n27 Place Aguesseau\nFETCH NETWORK\n"
    pc = classify("FACTURE INV-000035-26\n" + text, "INV-000035-26.pdf")
    assert "Place" not in pc.counterparty


def test_our_own_name_in_the_addressee_block_yields_no_client():
    """If we are the addressee, the document was issued to us, not by us."""
    text = "Adresse de facturation\nCybesis Studios\n1015 Avenue Nina Simone\n"
    pc = classify("SARL NATURAPI SIRET : 34428220700025 facture\n" + text, "f.pdf")
    assert pc.doc_class == "invoice-received"
