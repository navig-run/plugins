"""Reading expiry dates out of documents: precise, or nothing."""

from __future__ import annotations

from datetime import date

import pytest
from navig_cabinet.dates import _check_digit, find_expiry

TODAY = date(2026, 9, 27)


def _td3_line2(expiry: str, *, corrupt: bool = False) -> str:
    """A passport MRZ line 2 with a correct (or deliberately wrong) expiry check digit."""
    doc, nat, dob, sex = "12AB34567", "FRA", "900101", "F"
    check = _check_digit(expiry)
    if corrupt:
        check = (check + 1) % 10
    line = f"{doc}{_check_digit(doc)}{nat}{dob}{_check_digit(dob)}{sex}{expiry}{check}"
    return line.ljust(44, "<")


def _td1_line2(expiry: str) -> str:
    dob = "850615"
    return f"{dob}{_check_digit(dob)}M{expiry}{_check_digit(expiry)}FRA".ljust(30, "<")


@pytest.mark.parametrize("text,expected", [
    ("CARTE NATIONALE D'IDENTITÉ\nDate d'expiration : 14/03/2031", "2031-03-14"),
    ("Date d’expiration 14.03.2031", "2031-03-14"),
    ("PASSPORT\nDate of expiry / Date d'expiration\n12 MAR 2029", "2029-03-12"),
    ("Titre de séjour — valable jusqu'au 05 février 2028", "2028-02-05"),
    ("Expiry date: 2030-11-30", "2030-11-30"),
    ("Gültig bis 01.07.2032", "2032-07-01"),
    ("Assurance habitation — fin de validité 31/12/2026", "2026-12-31"),
])
def test_labelled_dates(text, expected):
    found = find_expiry(text, today=TODAY)
    assert found and found.expires == expected and found.source == "label"


def test_the_passport_mrz_wins_and_is_check_digit_verified():
    text = "P<FRAMARTIN<<JEANNE<<<<<<<<<<<<<<<<<<<<<<<<<\n" + _td3_line2("310115")
    found = find_expiry(text, today=TODAY)
    assert found and (found.expires, found.source) == ("2031-01-15", "mrz")


def test_an_id_card_td1_mrz():
    text = "IDFRAX1234567<<<<<<<<<<<<<<<\n" + _td1_line2("330620") + "\nMARTIN<<JEANNE<<<<<<<<<<<<<<"
    found = find_expiry(text, today=TODAY)
    assert found and found.expires == "2033-06-20"


def test_a_misread_mrz_is_rejected_not_trusted():
    """OCR swaps one digit — the check digit catches it, and without a label: nothing."""
    text = "P<FRAMARTIN<<JEANNE<<<<<<<<<<<<<<<<<<<<<<<<<\n" + _td3_line2("310115", corrupt=True)
    assert find_expiry(text, today=TODAY) is None


@pytest.mark.parametrize("text", [
    "Né(e) le 14/03/1990 à Lyon",                        # a birth date is not an expiry
    "Date de délivrance 14/03/2021",                      # nor is an issue date
    "Résultats du 12/03/2025 — hémoglobine 13,2 g/dL",    # a medical record's date
    "Date d'expiration : 14/03/1999",                     # implausibly old
    "Date d'expiration : 14/03/2099",                     # implausibly far
    "Date d'expiration : 31/02/2030",                     # not a real day
    "",
])
def test_nothing_is_guessed(text):
    assert find_expiry(text, today=TODAY) is None


def test_the_expiry_is_taken_not_the_issue_date_beside_it():
    text = "Date de délivrance 14/03/2021   Date d'expiration 13/03/2031"
    assert find_expiry(text, today=TODAY).expires == "2031-03-13"


# ── wired into add, edit and `navig cabinet dates` ───────────────────────────


def _cli(*args, env=None):
    from navig_cabinet.commands.cabinet import cabinet_app
    from typer.testing import CliRunner

    return CliRunner().invoke(cabinet_app, [str(a) for a in args], env=env)


def _json(res):
    import json

    return json.loads(res.output[res.output.index(next(c for c in res.output if c in "[{")):])


def _stub_text(monkeypatch, text):
    import navig_cabinet.ingest as ingest
    from navig_cabinet.extract import TextResult

    monkeypatch.setattr(ingest, "extract_text", lambda p, k, transcribe=False: TextResult(text, "tesseract"))


def test_add_reads_the_expiry_and_marks_it_detected(tmp_path, monkeypatch):
    _stub_text(monkeypatch, "Carte nationale d'identité — Date d'expiration : 14/03/2031")
    f = tmp_path / "cni.png"
    f.write_bytes(b"x")
    item = _json(_cli("add", f, "--json"))["added"][0]
    assert item["expires"] == "2031-03-14" and item["expires_detected"] is True
    assert "read from the document" in _cli("show", item["id"]).output
    # a typed date is the operator's own
    _cli("edit", item["id"], "--expires", "2031-03-15")
    got = _json(_cli("show", item["id"], "--json"))
    assert got["expires"] == "2031-03-15" and got["expires_detected"] is False


def test_an_explicit_expires_is_never_overridden(tmp_path, monkeypatch):
    _stub_text(monkeypatch, "Date d'expiration : 14/03/2031")
    f = tmp_path / "cni.png"
    f.write_bytes(b"y")
    item = _json(_cli("add", f, "--expires", "2030-01-01", "--json"))["added"][0]
    assert item["expires"] == "2030-01-01" and item["expires_detected"] is False


def test_dates_finds_then_applies_for_items_that_have_none(tmp_path, monkeypatch):
    _stub_text(monkeypatch, "PASSPORT — Date of expiry 12 MAR 2029")
    f = tmp_path / "passport.png"
    f.write_bytes(b"z")
    _cli("add", f, "--no-ocr")          # stored without text → no date
    # give it text the way a re-OCR would, then ask `dates`
    from navig_cabinet.store import Cabinet, default_root

    with Cabinet.open(default_root()) as c:
        it = c.items()[0]
        c._db.execute("DELETE FROM items WHERE id = ?", (it.id,))
        c._db.commit()
    _cli("add", f)                       # re-added WITH text (stub) → detected on add
    assert _json(_cli("dates", "--json")) == []   # nothing left without a date
    with Cabinet.open(default_root()) as c:
        it = c.items()[0]
        c.update(it.id, expires=None, expires_detected=False)
    found = _json(_cli("dates", "--json"))
    assert [(d["expires"], d["source"]) for d in found] == [("2029-03-12", "label")]
    assert _json(_cli("list", "--json"))[0]["expires"] is None, "a dry run saves nothing"
    assert _cli("dates", "--apply").exit_code == 0
    got = _json(_cli("list", "--json"))[0]
    assert got["expires"] == "2029-03-12" and got["expires_detected"] is True


def test_old_items_without_the_field_still_load(cab, tmp_path):
    """Items stored before this field existed have no `expires_detected` in their meta."""
    f = tmp_path / "old.pdf"
    f.write_bytes(b"old")
    item = cab.add_stream(f.open("rb"), original_name="old.pdf", kind="pdf", mime="x",
                          category="other")
    dek = cab._get_with_dek(item.id)[1]
    meta = {k: getattr(item, k) for k in ("title", "original_name", "kind", "mime", "category",
                                           "size", "sha256", "added_at", "tags", "expires",
                                           "issuer", "notes", "text", "text_source")}
    cab._db.execute("UPDATE items SET meta = ? WHERE id = ?", (cab._seal_meta(dek, item.id, meta), item.id))
    cab._db.commit()
    assert cab.get(item.id).expires_detected is False


# ── a labelled date the document QUOTES is not its expiry ─────────────────────
#
# Measured 2026-09-27: a France Travail registration receipt, filed that day,
# carries "Date fin de validité du titre : 12/07/2025" — the expiry of the residence
# permit it mentions. It was read as the receipt's own expiry and the operator got
# "a financial document expired 443 day(s) ago", with nothing to renew.

RECEIPT = (
    "Récapitulatif de la demande d'inscription du 07 juillet 2025\n"
    "Nationalité : ukrainienne\n"
    "Date fin de validité du titre : 12/07/2025\n"
)


def test_a_long_past_labelled_date_is_a_quote_not_an_expiry():
    assert find_expiry(RECEIPT, today=TODAY) is None


def test_a_recently_lapsed_document_still_counts():
    """An insurance certificate that ran out last month is a real renewal."""
    found = find_expiry("Date d'expiration : 15/08/2026", today=TODAY)
    assert found is not None and found.expires == "2026-08-15"


def test_the_scan_moves_past_a_quoted_date_to_the_documents_own():
    text = RECEIPT + "Date d'expiration : 14/03/2031"
    assert find_expiry(text, today=TODAY).expires == "2031-03-14"


def test_an_expired_passport_is_still_read_from_its_machine_readable_zone():
    """The MRZ expiry is check-digit verified and IS the document's own — a
    passport that lapsed years ago is exactly what to renew."""
    line = _td3_line2("200101")  # expired 2020-01-01
    found = find_expiry(f"P<UTOERIKSSON<<ANNA<MARIA\n{line}", today=TODAY)
    assert found is not None and found.source == "mrz" and found.expires == "2020-01-01"
