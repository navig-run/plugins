"""Duplicate resolution and destination naming."""

from __future__ import annotations

from navig_cabinet.paperwork import naming
from navig_cabinet.paperwork.dedupe import elect_keeper, exact_groups, near_groups
from navig_cabinet.paperwork.plan import PlanRow


def row(src: str, sha: str = "a" * 64, size: int = 100, mtime: float = 1.0, **kw) -> PlanRow:
    return PlanRow(src=src, sha256=sha, size=size, mtime=mtime, **kw)


# ── exact duplicates ────────────────────────────────────────────────────────


def test_five_identical_invoices_form_one_group():
    rows = [
        row(r"H:\docs\pdf\Factures-Invoices\INV-000022-25.pdf"),
        row(r"H:\docs\pdf\Factures-Invoices\INV-000022-25 (1).pdf"),
        row(r"H:\docs\pdf\Factures-Invoices\INV-000022-25 (2).pdf"),
        row(r"H:\docs\Client-Business-Docs\INV-000022-25.pdf"),
        row(r"H:\docs\Client-Business-Docs\INV-000022-25 (3).pdf"),
    ]
    groups = exact_groups(rows)
    assert len(groups) == 1 and len(next(iter(groups.values()))) == 5


def test_the_canonically_named_copy_is_elected_keeper():
    rows = [
        row(r"H:\docs\pdf\Factures-Invoices\INV-000022-25 (1).pdf"),
        row(r"H:\docs\pdf\Factures-Invoices\INV-000022-25.pdf"),
    ]
    keeper = elect_keeper(rows, [r"H:\docs"])
    assert keeper.src.endswith("INV-000022-25.pdf")


def test_keeper_election_is_deterministic_under_input_reordering():
    """A rescan must reproduce the same keeper, or a second apply migrates a twin."""
    paths = [
        r"H:\docs\a\urssaf-attestation-affiliation-20221224.pdf",
        r"H:\docs\b\urssaf-attestation-affiliation-20230310.pdf",
        r"H:\docs\c\77240796-8F94.pdf",
    ]
    first = elect_keeper([row(p) for p in paths], [r"H:\docs"]).src
    second = elect_keeper([row(p) for p in reversed(paths)], [r"H:\docs"]).src
    assert first == second


def test_the_earlier_source_root_wins():
    rows = [
        row(r"C:\Users\you\Downloads\INV-000032-26.pdf"),
        row(r"H:\docs\pdf\Factures-Invoices\INV-000032-26.pdf"),
    ]
    keeper = elect_keeper(rows, [r"H:\docs", r"C:\Users\you\Downloads"])
    assert keeper.src.startswith("H:")


# ── near duplicates ─────────────────────────────────────────────────────────


def test_a_compressed_variant_is_a_near_duplicate_not_an_exact_one():
    rows = [
        row(r"H:\docs\_unsorted\20230326220945.pdf", sha="1" * 64, size=5_980_000),
        row(r"H:\docs\_unsorted\20230326220945_compressed.pdf", sha="2" * 64, size=1_200_000),
    ]
    assert not exact_groups(rows)
    assert len(near_groups(rows)) == 1


def test_two_duplicata_with_the_same_stem_are_flagged_not_merged():
    """`(2)` is a DUPLICATA from October, `(3)` from July — different documents."""
    rows = [
        row(r"H:\docs\Facture F5520198601 (2).pdf", sha="3" * 64, size=40_000),
        row(r"H:\docs\Facture F5520198601 (3).pdf", sha="4" * 64, size=41_000),
    ]
    assert len(near_groups(rows)) == 1
    assert not exact_groups(rows)


def test_promesse_dembauche_and_sergeim_are_never_grouped():
    rows = [
        row(r"H:\docs\Promesse d embauche Sergei.pdf", sha="5" * 64, size=282_000),
        row(r"H:\docs\Promesse d embauche SergeiM.pdf", sha="6" * 64, size=101_000),
    ]
    assert near_groups(rows) == {}


# ── destinations ────────────────────────────────────────────────────────────


def test_issued_destination_keeps_the_legal_identifier_verbatim():
    r = row(r"H:\x\INV-000022-25.pdf", doc_class="invoice-issued",
            doc_id="INV-000022-25", doc_date="2025-07-16", counterparty="cyberaigen")
    dest = naming.destination(r)
    assert dest == "finance/invoices/issued/2025/2025-07-16-INV-000022-25-cyberaigen.pdf"


def test_received_invoice_files_under_its_vendor_and_year():
    r = row(r"H:\x\bt.pdf", sha="f" * 64, doc_class="invoice-received",
            doc_date="2023-07-20", counterparty="bouygues")
    assert naming.destination(r).startswith("finance/invoices/received/2023/2023-07-20-bouygues-")


def test_a_handoff_row_can_never_receive_a_destination():
    r = row(r"H:\x\Carte_Identite.pdf", doc_class="personal-handoff")
    assert naming.destination(r) == ""


def test_an_undated_document_is_filed_under_undated_not_a_guessed_year():
    r = row(r"H:\x\mystery.pdf", doc_class="tax-social", doc_date="")
    assert "/undated/" in naming.destination(r)


def test_a_non_latin_filename_still_yields_an_addressable_destination():
    r = row(r"H:\x\СВИДЕТЕЛЬСТВО.pdf", sha="9" * 64, doc_class="unknown", doc_date="2024-01-01")
    dest = naming.destination(r)
    assert dest.endswith(".pdf") and dest.rsplit("/", 1)[-1] != ".pdf"


def test_destinations_are_unique_across_a_realistic_batch():
    rows = [
        row(rf"H:\x\INV-0000{n}-25.pdf", sha=str(n) * 64, doc_class="invoice-issued",
            doc_id=f"INV-0000{n}-25", doc_date="2025-01-0{}".format(n % 9 + 1))
        for n in range(20, 30)
    ]
    dests = [naming.destination(r) for r in rows]
    assert len(set(dests)) == len(dests)
