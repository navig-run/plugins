"""End-to-end paper intake: a letter dropped in inbox/ → scanned → filed → radar.

Runs the real CLI against a temp space passed by PATH (no registry), with a text
letter (always readable) and — when local OCR is available — a rendered PNG of one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from navig_cabinet.paperwork.commands.paperwork import paperwork_app
from navig_cabinet.paperwork.extract import (
    extract_text,
    missing_extractors,
    ocr_unavailable_reason,
)
from navig_cabinet.paperwork.plan import scan
from typer.testing import CliRunner

runner = CliRunner()

# `scan` refuses to run without the document readers (a filename-only plan would file a
# medical bill as an invoice). That gate is product behaviour; these end-to-end tests
# need the readers present, as they are on the supported interpreters.
needs_readers = pytest.mark.skipif(
    bool(missing_extractors()),
    reason=f"document readers missing: {missing_extractors()}",
)

CAF = """CAISSE D ALLOCATIONS FAMILIALES DE L HERAULT
Montpellier, le 12 septembre 2026
N de dossier : 1234567A
Objet : Aide personnalisee au logement - pieces manquantes
Monsieur,
Afin de poursuivre l etude de votre dossier APL, merci de nous retourner avant le
30 septembre 2026 la quittance de loyer.
"""


@pytest.fixture
def space(tmp_path) -> Path:
    root = tmp_path / "paperwork-space"
    (root / ".navig").mkdir(parents=True)
    (root / ".navig" / "config.yaml").write_text(
        "paperwork:\n  profile: personal\nrenewal_alert_days: 30\n", encoding="utf-8"
    )
    (root / "inbox").mkdir()
    return root


def _run(*args: str):
    return runner.invoke(paperwork_app, list(args))


def _payload(output: str) -> dict:
    """The --json document (rich pretty-prints it over several lines)."""
    start = output.index("{")
    return json.loads(output[start:])


@needs_readers
def test_scan_apply_files_the_letter_and_records_the_mailroom(space, monkeypatch):
    (space / "inbox" / "IMG_2026.txt").write_text(CAF, encoding="utf-8")
    sent: list[str] = []
    import navig.messaging.notify_operator as no

    monkeypatch.setattr(
        no, "notify_operator", lambda text, **kw: sent.append(text) or True
    )

    res = _run(
        "scan",
        str(space / "inbox"),
        "--space",
        str(space),
        "--apply",
        "--yes",
        "--send",
        "--json",
    )
    assert res.exit_code == 0, res.output
    payload = _payload(res.output)
    assert payload["migrated"] == 1
    assert payload["courrier_ledger_new"] == 1 and payload["echeances_new"] == 1

    filed = list((space / "personal" / "logement" / "2026").glob("*.txt"))
    assert len(filed) == 1
    assert filed[0].name.startswith("2026-09-12-caf-aide-personnalisee-au-logement")
    assert not (space / "inbox" / "IMG_2026.txt").exists()  # quarantined, not deleted
    assert (space / ".navig" / "paperwork" / ".trash").exists()

    ledger = (space / "mailroom" / "ledger" / "courrier.jsonl").read_text(
        encoding="utf-8"
    )
    assert '"emetteur_label": "CAF"' in ledger and "1234567A" not in ledger
    assert sent and "CAF" in sent[0] and "1234567A" not in sent[0]

    # The radar sees the deadline and writes the report.
    res = _run("echeances", "--space", str(space), "--json")
    assert res.exit_code == 0, res.output
    radar = _payload(res.output)
    assert radar["count"] == 1 and radar["items"][0]["due"] == "2026-09-30"
    assert (space / "mailroom" / "reports" / "echeancier.md").exists()

    # An empty inbox is an answer, not an error — and a no-op for cron.
    res = _run(
        "scan",
        str(space / "inbox"),
        "--space",
        str(space),
        "--apply",
        "--yes",
        "--json",
    )
    assert res.exit_code == 0, res.output
    assert _payload(res.output)["scanned"] == 0


@needs_readers
def test_apply_needs_yes(space):
    (space / "inbox" / "l.txt").write_text(CAF, encoding="utf-8")
    res = _run("scan", str(space / "inbox"), "--space", str(space), "--apply")
    assert res.exit_code == 2
    assert not (space / "personal").exists()


def test_unknown_profile_is_a_usage_error(space):
    res = _run("scan", str(space / "inbox"), "--space", str(space), "--profile", "nope")
    assert res.exit_code == 2


@needs_readers
def test_company_paperwork_found_in_the_personal_inbox_is_handed_off_not_filed(space):
    (space / "inbox" / "urssaf.txt").write_text(
        "URSSAF\nCotisations sociales des travailleurs independants\nSIRET 753 204 742 00047\n",
        encoding="utf-8",
    )
    res = _run("scan", str(space / "inbox"), "--space", str(space), "--json")
    assert res.exit_code == 0, res.output
    payload = _payload(res.output)
    assert payload["handoff"] == 1 and payload["decisions"].get("handoff") == 1
    manifest = (space / ".navig" / "paperwork" / "handoff.jsonl").read_text(
        encoding="utf-8"
    )
    assert '"suggested_space": "company"' in manifest


def _render_png(path: Path, text: str) -> bool:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return False
    img = Image.new("RGB", (1400, 700), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 36)
    except OSError:
        font = ImageFont.load_default()
    y = 40
    for line in text.splitlines():
        draw.text((40, y), line, fill="black", font=font)
        y += 52
    img.save(path)
    return True


@pytest.mark.skipif(
    ocr_unavailable_reason() is not None, reason="local OCR (tesseract) unavailable"
)
def test_a_photographed_letter_is_read_by_local_ocr(space):
    png = space / "inbox" / "IMG_0001.png"
    if not _render_png(
        png,
        "CAISSE D ALLOCATIONS FAMILIALES\nObjet : Aide au logement\nmerci de nous retourner avant le 30 septembre 2026 la quittance de loyer",
    ):
        pytest.skip("Pillow unavailable to render the fixture")

    ex = extract_text(png)
    assert "tesseract" in ex.method or ex.method == "ocr", ex
    assert "logement" in ex.text.lower()

    rows = scan([space / "inbox"], profile="personal")
    assert len(rows) == 1
    row = rows[0]
    assert row.doc_class == "courrier-logement", (row.doc_class, row.signals, row.notes)
    assert row.emetteur == "caf"
