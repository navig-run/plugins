"""Apply and undo: idempotence, resumability, and refusing to lose a document."""

from __future__ import annotations

import json

import pytest
from navig_cabinet.paperwork import apply as apply_mod
from navig_cabinet.paperwork.apply import apply_plan
from navig_cabinet.paperwork.plan import PlanRow
from navig_cabinet.paperwork.space import PaperworkPaths
from navig_cabinet.paperwork.undo import undo


@pytest.fixture
def space(tmp_path):
    root = tmp_path / "company-space"
    root.mkdir()
    return PaperworkPaths(root)


@pytest.fixture
def source(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    return src


def make_row(path, dest_rel, decision="migrate", **kw):
    from navig_cabinet.paperwork.extract import sha256_file

    return PlanRow(
        row_id=path.stem, decision=decision, doc_class="invoice-issued",
        confidence=0.99, dest_rel=dest_rel, src=str(path),
        size=path.stat().st_size, sha256=sha256_file(path), **kw
    )


def test_apply_copies_verifies_then_quarantines_the_source(space, source):
    f = source / "INV-000022-25.pdf"
    f.write_bytes(b"invoice payload")
    row = make_row(f, "finance/invoices/issued/2025/INV-000022-25.pdf")

    stats, receipt = apply_plan([row], space)

    assert stats.migrated == 1 and not stats.failed
    assert (space.space_root / row.dest_rel).read_bytes() == b"invoice payload"
    assert not f.exists(), "source should have moved to quarantine"
    assert list(space.trash_root.rglob("INV-000022-25.pdf")), "quarantine copy missing"
    assert receipt.exists()


def test_a_dry_run_touches_nothing(space, source):
    f = source / "a.pdf"
    f.write_bytes(b"x")
    row = make_row(f, "finance/invoices/issued/2025/a.pdf")

    stats, receipt = apply_plan([row], space, dry_run=True)

    assert stats.migrated == 1
    assert f.exists() and not (space.space_root / row.dest_rel).exists()
    assert receipt is None


def test_a_second_apply_is_a_no_op(space, source):
    f = source / "b.pdf"
    f.write_bytes(b"payload")
    row = make_row(f, "finance/invoices/issued/2025/b.pdf")

    apply_plan([row], space)
    stats, _ = apply_plan([row], space)

    assert stats.migrated == 0 and stats.skipped == 1 and not stats.failed


def test_apply_resumes_after_an_interrupted_run(space, source):
    files = []
    for n in range(3):
        f = source / f"c{n}.pdf"
        f.write_bytes(f"payload {n}".encode())
        files.append(f)
    rows = [make_row(f, f"finance/invoices/issued/2025/{f.name}") for f in files]

    apply_plan(rows, space, limit=1)          # interrupted after one
    stats, _ = apply_plan(rows, space)         # resume

    assert stats.migrated == 2 and stats.skipped == 1
    for r in rows:
        assert (space.space_root / r.dest_rel).exists()


def test_a_corrupted_copy_keeps_the_source(space, source, monkeypatch):
    f = source / "d.pdf"
    f.write_bytes(b"important")
    row = make_row(f, "finance/invoices/issued/2025/d.pdf")

    monkeypatch.setattr(apply_mod, "_copy_verify", lambda src, tmp: "")

    stats, _ = apply_plan([row], space)

    assert stats.mismatch == 1 and stats.failed
    assert f.exists(), "a failed verification must never cost the original"
    assert not (space.space_root / row.dest_rel).exists()


def test_a_destination_collision_is_reported_not_overwritten(space, source):
    dest = space.space_root / "finance/invoices/issued/2025/e.pdf"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"existing different content")

    f = source / "e.pdf"
    f.write_bytes(b"new content")
    row = make_row(f, "finance/invoices/issued/2025/e.pdf")

    stats, _ = apply_plan([row], space)

    assert stats.collision == 1 and stats.failed
    assert dest.read_bytes() == b"existing different content"
    assert f.exists()


def test_handoff_sources_are_untouched(space, source):
    f = source / "Carte_Identite.pdf"
    f.write_bytes(b"private")
    row = make_row(f, "", decision="handoff")
    row.doc_class = "personal-handoff"

    stats, _ = apply_plan([row], space)

    assert stats.handoff == 1 and stats.migrated == 0
    assert f.read_bytes() == b"private"


def test_review_rows_are_skipped(space, source):
    f = source / "maybe.pdf"
    f.write_bytes(b"?")
    row = make_row(f, "docs/unsorted/2025/maybe.pdf", decision="review")

    stats, _ = apply_plan([row], space)

    assert stats.review == 1 and f.exists()


def test_a_destination_escaping_the_space_is_refused(space, source):
    """The cardinal-rule guard, reachable only via a hand-edited dest_rel."""
    f = source / "leak.pdf"
    f.write_bytes(b"x")
    row = make_row(f, "../human-space/ops/leak.pdf")

    stats, _ = apply_plan([row], space)

    assert stats.migrated == 0 and stats.failed
    assert f.exists()


def test_a_duplicate_is_quarantined_without_being_copied(space, source):
    f = source / "dupe.pdf"
    f.write_bytes(b"dup")
    row = make_row(f, "", decision="trash-dupe")

    stats, _ = apply_plan([row], space)

    assert stats.trashed_dupe == 1
    assert not f.exists()
    assert list(space.trash_root.rglob("dupe.pdf"))


# ── undo ────────────────────────────────────────────────────────────────────


def test_undo_restores_every_migrated_source(space, source):
    f = source / "g.pdf"
    f.write_bytes(b"restore me")
    row = make_row(f, "finance/invoices/issued/2025/g.pdf")
    _, receipt = apply_plan([row], space)

    stats = undo(receipt)

    assert stats.restored == 1 and not stats.failed
    assert f.read_bytes() == b"restore me"
    assert not (space.space_root / row.dest_rel).exists()


def test_undo_is_idempotent(space, source):
    f = source / "h.pdf"
    f.write_bytes(b"twice")
    row = make_row(f, "finance/invoices/issued/2025/h.pdf")
    _, receipt = apply_plan([row], space)

    undo(receipt)
    second = undo(receipt)

    assert second.restored == 0 and second.already == 1 and not second.failed


def test_undo_refuses_a_destination_edited_after_filing(space, source):
    """Deleting an edited destination would destroy work that exists nowhere else."""
    f = source / "i.pdf"
    f.write_bytes(b"original")
    row = make_row(f, "finance/invoices/issued/2025/i.pdf")
    _, receipt = apply_plan([row], space)

    (space.space_root / row.dest_rel).write_bytes(b"edited since filing")

    stats = undo(receipt)

    assert stats.refused == 1 and stats.failed
    assert (space.space_root / row.dest_rel).read_bytes() == b"edited since filing"


def test_a_receipt_records_enough_to_reverse_each_action(space, source):
    f = source / "j.pdf"
    f.write_bytes(b"payload")
    row = make_row(f, "finance/invoices/issued/2025/j.pdf")
    _, receipt = apply_plan([row], space)

    entry = json.loads(receipt.read_text(encoding="utf-8").splitlines()[0])
    assert {"src", "dest", "trash", "sha256", "action"} <= set(entry)
