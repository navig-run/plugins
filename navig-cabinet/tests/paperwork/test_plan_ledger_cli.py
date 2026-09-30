"""Plan round-trip, the handoff manifest, the ledger, and CLI exit codes."""

from __future__ import annotations

import json

import pytest
from navig_cabinet.paperwork import handoff, ledger
from navig_cabinet.paperwork.commands.paperwork import paperwork_app
from navig_cabinet.paperwork.plan import PlanRow, read_csv, summarize, write_csv
from navig_cabinet.paperwork.space import PaperworkPaths
from typer.testing import CliRunner

runner = CliRunner()


@pytest.fixture
def space(tmp_path):
    root = tmp_path / "company-space"
    root.mkdir()
    return PaperworkPaths(root)


# ── plan CSV ────────────────────────────────────────────────────────────────


def test_a_plan_round_trips_through_csv(tmp_path):
    rows = [
        PlanRow(row_id="a1", decision="migrate", doc_class="invoice-issued",
                confidence=0.99, dest_rel="finance/invoices/issued/2025/x.pdf",
                src=r"H:\x.pdf", size=10, sha256="f" * 64, doc_id="INV-000022-25"),
        PlanRow(row_id="b2", decision="handoff", doc_class="personal-handoff",
                confidence=0.95, src=r"H:\y.pdf", veto_subclass="health"),
    ]
    path = tmp_path / "plan.csv"
    write_csv(rows, path)
    back = read_csv(path)

    assert [r.row_id for r in back] == ["a1", "b2"]
    assert back[0].confidence == 0.99 and back[0].doc_id == "INV-000022-25"
    assert back[1].veto_subclass == "health"


def test_a_hand_edited_decision_survives_the_round_trip(tmp_path):
    """The plan is the review surface: edits in Excel must be honoured."""
    path = tmp_path / "plan.csv"
    write_csv([PlanRow(row_id="a", decision="review", src=r"H:\a.pdf")], path)

    text = path.read_text(encoding="utf-8-sig").replace("review", "migrate")
    path.write_text(text, encoding="utf-8-sig")

    assert read_csv(path)[0].decision == "migrate"


def test_a_plan_is_written_utf8_sig_so_excel_renders_accents(tmp_path):
    path = tmp_path / "plan.csv"
    write_csv([PlanRow(row_id="a", src="Avis d'échéance.pdf")], path)
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")


def test_summarize_counts_by_decision():
    rows = [PlanRow(decision=d) for d in ("migrate", "migrate", "review", "handoff")]
    assert summarize(rows) == {"handoff": 1, "migrate": 2, "review": 1}


# ── handoff manifest ────────────────────────────────────────────────────────


def test_handoff_rows_are_never_given_a_destination(space):
    """Structural guarantee: no code path gives a personal document a place in a space."""
    rows = [PlanRow(row_id="h1", decision="handoff", doc_class="personal-handoff",
                    veto_subclass="health", src=r"H:\note.pdf", original_name="note.pdf")]
    handoff.write(rows, space.handoff_jsonl, space.handoff_md)
    assert all(not r.dest_rel for r in rows)


def test_the_manifest_names_the_destination_space_without_writing_to_it(space):
    rows = [
        PlanRow(row_id="h1", decision="handoff", veto_subclass="health",
                src=r"H:\note.pdf", original_name="note.pdf"),
        PlanRow(row_id="h2", decision="handoff", veto_subclass="identity",
                src=r"H:\cni.pdf", original_name="cni.pdf"),
    ]
    n = handoff.write(rows, space.handoff_jsonl, space.handoff_md)

    assert n == 2
    body = space.handoff_md.read_text(encoding="utf-8")
    # Medical and identity documents belong to the encrypted cabinet, not to any space.
    assert "navig cabinet" in body
    assert "human-health-space" not in body and "personal/identite/" not in body
    assert space.handoff_jsonl.exists()
    entries = [json.loads(line) for line in space.handoff_jsonl.read_text(encoding="utf-8").splitlines()]
    assert all(e["suggested_space"] is None and e["suggested_tool"] == "navig cabinet" for e in entries)


def test_a_secret_routes_to_a_tool_rather_than_any_space(space):
    entry = handoff.entry_for(PlanRow(veto_subclass="secret", src=r"H:\2fa.txt"))
    assert entry["suggested_space"] is None
    assert entry["suggested_tool"] == "navig vault"
    assert entry["never_migrate"] is True


# ── ledger ──────────────────────────────────────────────────────────────────


def _place(space, rel, payload=b"x"):
    p = space.space_root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(payload)


def test_the_ledger_derives_the_next_invoice_id_from_the_real_series(space):
    _place(space, "finance/invoices/issued/2026/2026-05-31-INV-000032-26.pdf")
    _place(space, "finance/invoices/issued/2025/2025-12-27-INV-000027-25.pdf")

    docs = ledger.build(space)
    assert ledger.next_invoice_id(docs).startswith("INV-000033-")


def test_the_ledger_reports_gaps_in_the_issued_series(space):
    for n, year in ((18, 2023), (19, 2023), (21, 2023)):
        _place(space, f"finance/invoices/issued/{year}/INV-0000{n}-23.pdf", str(n).encode())

    docs = ledger.build(space)
    assert ledger.series_gaps(docs) == [20]


def test_the_ledger_is_rebuildable_from_disk_alone(space):
    _place(space, "finance/invoices/issued/2026/INV-000032-26.pdf")
    first = ledger.write(space, ledger.build(space))
    second = ledger.write(space, ledger.build(space))
    assert first == second


# ── CLI exit codes ──────────────────────────────────────────────────────────


def test_help_exits_zero():
    assert runner.invoke(paperwork_app, ["--help"]).exit_code == 0


def test_scan_with_an_unknown_space_exits_2(tmp_path):
    result = runner.invoke(paperwork_app, ["scan", str(tmp_path), "--space", "no-such-space"])
    assert result.exit_code == 2


def test_scan_with_a_missing_source_exits_2(monkeypatch, space):
    monkeypatch.setattr("navig_cabinet.paperwork.space.paths_for", lambda s: space)
    result = runner.invoke(paperwork_app, ["scan", r"Z:\nope", "--space", "company"])
    assert result.exit_code == 2


def test_apply_without_a_plan_exits_2(monkeypatch, space):
    monkeypatch.setattr("navig_cabinet.paperwork.space.paths_for", lambda s: space)
    result = runner.invoke(paperwork_app, ["apply", "--space", "company", "--yes"])
    assert result.exit_code == 2


def test_undo_without_a_receipt_exits_2(monkeypatch, space):
    monkeypatch.setattr("navig_cabinet.paperwork.space.paths_for", lambda s: space)
    result = runner.invoke(paperwork_app, ["undo", "--space", "company"])
    assert result.exit_code == 2


def test_index_on_an_unfiled_space_exits_0(monkeypatch, space):
    """Nothing to do is not a failure."""
    monkeypatch.setattr("navig_cabinet.paperwork.space.paths_for", lambda s: space)
    result = runner.invoke(paperwork_app, ["index", "--space", "company"])
    assert result.exit_code == 0


def test_review_on_an_empty_plan_exits_0(monkeypatch, space):
    monkeypatch.setattr("navig_cabinet.paperwork.space.paths_for", lambda s: space)
    write_csv([], space.plan_csv)
    result = runner.invoke(paperwork_app, ["review", "--space", "company"])
    assert result.exit_code == 0


def test_apply_with_nothing_actionable_exits_0(monkeypatch, space):
    monkeypatch.setattr("navig_cabinet.paperwork.space.paths_for", lambda s: space)
    write_csv([PlanRow(row_id="a", decision="review", src=r"H:\a.pdf")], space.plan_csv)
    result = runner.invoke(paperwork_app, ["apply", "--space", "company", "--yes"])
    assert result.exit_code == 0


def test_apply_without_yes_changes_nothing_and_exits_0(monkeypatch, space, tmp_path):
    monkeypatch.setattr("navig_cabinet.paperwork.space.paths_for", lambda s: space)
    src = tmp_path / "x.pdf"
    src.write_bytes(b"payload")
    write_csv(
        [PlanRow(row_id="a", decision="migrate", src=str(src), sha256="f" * 64,
                 dest_rel="finance/invoices/issued/2025/x.pdf")],
        space.plan_csv,
    )
    result = runner.invoke(paperwork_app, ["apply", "--space", "company"])
    assert result.exit_code == 0
    assert src.exists() and not (space.space_root / "finance").exists()


# ── cabinet hint ────────────────────────────────────────────────────────────


def test_the_manifest_offers_the_encrypted_cabinet_for_personal_documents_only(space):
    """ID/medical entries name `navig cabinet`; benefits letters keep their space route."""
    personal = [PlanRow(row_id="h1", decision="handoff", veto_subclass="identity",
                        src=r"H:\cni.pdf", original_name="cni.pdf")]
    handoff.write(personal, space.handoff_jsonl, space.handoff_md)
    body = space.handoff_md.read_text(encoding="utf-8")
    assert "navig cabinet import-paperwork" in body
    assert space.handoff_jsonl.as_posix() in body
    assert "navig paperwork handoff --yes" in body

    other = [PlanRow(row_id="h2", decision="handoff", veto_subclass="benefits",
                     src=r"H:\caf.pdf", original_name="caf.pdf")]
    handoff.write(other, space.handoff_jsonl, space.handoff_md)
    assert "navig cabinet" not in space.handoff_md.read_text(encoding="utf-8")
