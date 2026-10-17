"""The invoice register: facts rebuilt from disk, judgements preserved."""

from __future__ import annotations

import csv

import pytest
from navig_cabinet.paperwork import register
from navig_cabinet.paperwork.ledger import Document
from navig_cabinet.paperwork.register import Entry
from navig_cabinet.paperwork.space import PaperworkPaths


@pytest.fixture
def space(tmp_path):
    root = tmp_path / "company-space"
    (root / "finance" / "invoices").mkdir(parents=True)
    return PaperworkPaths(root)


def doc(n, year, client, path=None):
    return Document(
        doc_id=f"INV-{n:06d}-{str(year)[2:]}", doc_class="invoice-issued",
        path=path or f"finance/invoices/issued/{year}/INV-{n:06d}-{str(year)[2:]}-{client}.pdf",
        sha256="f" * 64, doc_date=f"{year}-02-28", size=100,
        series_n=n, series_yy=str(year)[2:],
    )


# ── amount parsing ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [
    ("2.000,00", 2000.0),      # French
    ("2,000.00", 2000.0),      # Anglo
    ("4 000,00", 4000.0),      # French with a space separator
    ("2000", 2000.0),
    ("1.110,00", 1110.0),
    ("", 0.0),
    ("n/a", 0.0),
])
def test_amounts_parse_in_both_conventions(raw, expected):
    assert register._to_float(raw) == expected


# ── preservation ────────────────────────────────────────────────────────────


def test_a_recorded_payment_status_survives_a_rebuild(space):
    """The whole point: re-deriving facts must not erase your judgements."""
    first = [Entry(invoice_id="INV-000029-26", date="2026-02-28", client="cyberaigen",
                   amount_eur="2000", path="p.pdf", sha256="a" * 64)]
    register.write(space, first)

    # a human marks it unpaid, with a note
    csv_path = space.space_root / "finance/invoices/register.csv"
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8-sig")))
    rows[0]["status"] = "unpaid"
    rows[0]["notes"] = "client never paid"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(register.COLUMNS))
        w.writeheader()
        w.writerows(rows)

    rebuilt = register.build(space, [doc(29, 2026, "cyberaigen")])
    assert rebuilt[0].status == "unpaid"
    assert rebuilt[0].notes == "client never paid"


def test_facts_are_re_derived_not_preserved(space):
    """A wrong client in an old register must not survive; only judgements do."""
    register.write(space, [Entry(invoice_id="INV-000030-26", client="WRONG",
                                 amount_eur="999", status="paid")])
    rebuilt = register.build(space, [doc(30, 2026, "iaka")])
    assert rebuilt[0].client == "iaka"
    assert rebuilt[0].status == "paid"          # judgement kept


def test_only_issued_invoices_enter_the_register(space):
    docs = [doc(30, 2026, "iaka"),
            Document(doc_id="x", doc_class="invoice-received", path="p.pdf",
                     sha256="b" * 64, doc_date="2026-01-01", size=1)]
    assert len(register.build(space, docs)) == 1


# ── totals ──────────────────────────────────────────────────────────────────


def test_paid_and_invoiced_are_counted_separately():
    """Billing is not the taxable event for a micro-entrepreneur; being paid is."""
    entries = [
        Entry(invoice_id="a", amount_eur="2000", status="paid"),
        Entry(invoice_id="b", amount_eur="2000", status="unpaid"),
        Entry(invoice_id="c", amount_eur="500", status="unknown"),
    ]
    t = register.totals(entries)
    assert t.invoiced == 4500.0
    assert t.paid == 2000.0
    assert t.unpaid == 2000.0
    assert t.unknown == 500.0


def test_a_cancelled_invoice_counts_towards_neither_paid_nor_unpaid():
    t = register.totals([Entry(invoice_id="a", amount_eur="1000", status="cancelled")])
    assert t.paid == 0.0 and t.unpaid == 0.0 and t.unknown == 0.0


def test_the_markdown_names_the_unpaid_invoices(space):
    register.write(space, [
        Entry(invoice_id="INV-000029-26", client="cyberaigen", amount_eur="2000",
              status="unpaid", notes="february, never paid"),
        Entry(invoice_id="INV-000035-26", client="FETCH NETWORK", amount_eur="2000",
              status="paid"),
    ])
    body = (space.space_root / "finance/invoices/REGISTER.md").read_text(encoding="utf-8")
    assert "## Not paid" in body
    assert "INV-000029-26" in body
    assert "february, never paid" in body
    assert "encaissement" in body


def test_the_register_csv_opens_cleanly_in_excel(space):
    register.write(space, [Entry(invoice_id="a", client="Société Générale", amount_eur="1")])
    raw = (space.space_root / "finance/invoices/register.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")


# ── one row per invoice ─────────────────────────────────────────────────────


def test_one_invoice_filed_as_two_files_is_a_single_row(space):
    """INV-000017-21 is filed twice — as itself and as -CYBESIS-STUDIOS.

    Both files are real, but the invoice was billed once. Two rows double-count the
    revenue and make the annotation key ambiguous.
    """
    docs = [
        doc(17, 2021, "virtualplace", path="finance/invoices/issued/2021/INV-000017-21.pdf"),
        doc(17, 2021, "virtualplace",
            path="finance/invoices/issued/2021/INV-000017-21-CYBESIS-STUDIOS.pdf"),
    ]
    entries = register.build(space, docs)
    assert len(entries) == 1
    assert "also filed as" in entries[0].notes


def test_the_duplicate_path_is_recorded_rather_than_dropped(space):
    docs = [
        doc(17, 2021, "x", path="finance/invoices/issued/2021/a.pdf"),
        doc(17, 2021, "x", path="finance/invoices/issued/2021/b.pdf"),
    ]
    notes = register.build(space, docs)[0].notes
    assert "a.pdf" in notes or "b.pdf" in notes


def test_revenue_is_not_double_counted_by_a_duplicate_filing(space):
    docs = [
        doc(17, 2021, "x", path="finance/invoices/issued/2021/a.pdf"),
        doc(17, 2021, "x", path="finance/invoices/issued/2021/b.pdf"),
    ]
    entries = register.build(space, docs)
    for e in entries:
        e.amount_eur, e.status = "200,00", "paid"
    assert register.totals(entries).paid == 200.0


# ── amounts split across a line break ───────────────────────────────────────


def test_an_amount_whose_label_is_split_by_a_line_break_is_found():
    """A PDF text layer wraps mid-word: "Sub T\notal €2000T\notal €2000"."""
    from navig_cabinet.paperwork.classify import _amount
    assert _amount("Sub T\notal €2000T\notal €2000") == "2000"


def test_the_grand_total_wins_over_the_subtotal():
    from navig_cabinet.paperwork.classify import _amount
    assert _amount("Sous-total 100,00 Total 250,00") == "250,00"


def test_a_french_thousands_space_is_joined():
    from navig_cabinet.paperwork.classify import _amount
    assert _amount("Total HT 4 000,00 EUR") == "4000,00"


# ── the amount must be THIS invoice's amount ────────────────────────────────


def test_a_deposit_invoices_project_total_is_not_its_own_amount():
    """INV-000018-23 bills 1.110,00 and mentions a 3700 project total in a note.

    Taking the last figure on the page read the note as the invoice's amount.
    """
    from navig_cabinet.paperwork.classify import _amount
    flat = ("totalht 1.110,00 totalttc 1.110,00 "
            "note: ceci est une facture d'acompte sur le montant total de 3700")
    assert _amount(flat) == "1.110,00"


def test_subtotal_does_not_match_as_total():
    from navig_cabinet.paperwork.classify import _amount
    assert _amount("subtotal 1500 total 1500") == "1500"


def test_total_ttc_is_preferred_over_a_stray_later_number():
    """INV-000017-21 ends with an unrelated `14` after its real total."""
    from navig_cabinet.paperwork.classify import _amount
    text = "1 200,00 0% 200,00 total ht 200,00 total ttc 200,00 solde a payer 0,00 transact 14"
    assert _amount(text) == "200,00"


def test_a_single_digit_is_not_an_amount():
    from navig_cabinet.paperwork.classify import _amount
    assert _amount("total 1") == ""


def test_a_trailing_group_is_cents_or_thousands_and_nothing_else():
    from navig_cabinet.paperwork.classify import _amount
    assert _amount("total 1.234") == "1.234"      # French thousands
    assert _amount("total 250,00") == "250,00"    # cents
    assert _amount("total 1,2") == ""             # neither


def test_an_unlabelled_invoice_falls_back_to_a_currency_marker():
    from navig_cabinet.paperwork.classify import _amount
    assert _amount("Prestation €2000") == "2000"
