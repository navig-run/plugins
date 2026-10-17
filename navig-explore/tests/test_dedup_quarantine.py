"""Tests for `dedup --quarantine`.

The safety property that matters: a duplicate group must never lose every copy. Everything
else here is about not acting on classes where "duplicate" is a human judgement.
"""
from __future__ import annotations

import json

import pytest

from navig_explore import dedup, photos


def _write_dupes(root, groups):
    side = root / ".mediaexplorer"
    side.mkdir(parents=True, exist_ok=True)
    with (side / "dupes.jsonl").open("w", encoding="utf-8") as f:
        for g in groups:
            f.write(json.dumps(g) + "\n")


def _touch(p, text="x"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_plans_the_redundant_copies_and_keeps_the_keeper(tmp_path):
    root = tmp_path / "lib"
    _touch(root / "a.jpg"), _touch(root / "b.jpg"), _touch(root / "c.jpg")
    _write_dupes(root, [{"kind": "exact", "keep": "a.jpg",
                         "auto_trash": ["b.jpg", "c.jpg"]}])
    rows = dedup.plan_quarantine(root)
    assert {r["src"].split("\\")[-1].split("/")[-1] for r in rows} == {"b.jpg", "c.jpg"}
    assert all(".trash" in r["dst"] for r in rows)


def test_identical_pixels_is_quarantined_too(tmp_path):
    root = tmp_path / "lib"
    _touch(root / "a.jpg"), _touch(root / "b.jpg")
    _write_dupes(root, [{"kind": "identical-pixels", "keep": "a.jpg",
                         "auto_trash": ["b.jpg"]}])
    assert len(dedup.plan_quarantine(root)) == 1


@pytest.mark.parametrize("kind", ["near-image", "near-video"])
def test_near_clusters_are_never_quarantined(kind, tmp_path):
    """A near cluster is often a burst of consecutive shots, not one photo twice."""
    root = tmp_path / "lib"
    _touch(root / "a.jpg"), _touch(root / "b.jpg")
    _write_dupes(root, [{"kind": kind, "keep": "a.jpg", "auto_trash": ["b.jpg"]}])
    assert dedup.plan_quarantine(root) == []


def _names(rows):
    return {r["src"].replace("\\", "/").split("/")[-1] for r in rows}


def test_a_group_never_loses_every_copy(tmp_path):
    """Two groups naming each other's keeper must still leave one file standing."""
    root = tmp_path / "lib"
    _touch(root / "a.jpg"), _touch(root / "b.jpg")
    _write_dupes(root, [
        {"kind": "exact", "keep": "a.jpg", "members": ["a.jpg", "b.jpg"],
         "auto_trash": ["b.jpg"]},
        {"kind": "exact", "keep": "b.jpg", "members": ["a.jpg", "b.jpg"],
         "auto_trash": ["a.jpg"]},
    ])
    rows = dedup.plan_quarantine(root)
    assert len(rows) == 1, "exactly one of the two must go, never both"


def test_overlapping_groups_converge_in_ONE_pass(tmp_path):
    """The real case, found by running the CLI end to end.

    original.jpg is byte-identical to byte-twin.jpg AND pixel-identical to
    metadata-twin.jpg, so it is a keeper in one group and redundant in another. All
    three are the same photo: exactly one must survive after a single run.
    """
    root = tmp_path / "lib"
    _touch(root / "original.jpg"), _touch(root / "byte-twin.jpg")
    _touch(root / "sub" / "metadata-twin.jpg")
    _write_dupes(root, [
        {"kind": "exact", "keep": "original.jpg",
         "members": ["byte-twin.jpg", "original.jpg"], "auto_trash": ["byte-twin.jpg"]},
        {"kind": "identical-pixels", "keep": "sub/metadata-twin.jpg",
         "members": ["original.jpg", "sub/metadata-twin.jpg"],
         "auto_trash": ["original.jpg"]},
    ])
    rows = dedup.plan_quarantine(root)
    assert len(rows) == 2, "three copies of one photo -> two removed, one kept"
    survivors = {"original.jpg", "byte-twin.jpg", "metadata-twin.jpg"} - _names(rows)
    assert len(survivors) == 1


def test_chained_groups_collapse_to_a_single_survivor(tmp_path):
    """a~b and b~c means a, b and c are all the same image."""
    root = tmp_path / "lib"
    for n in ("a.jpg", "b.jpg", "c.jpg"):
        _touch(root / n)
    _write_dupes(root, [
        {"kind": "exact", "keep": "a.jpg", "members": ["a.jpg", "b.jpg"],
         "auto_trash": ["b.jpg"]},
        {"kind": "identical-pixels", "keep": "b.jpg", "members": ["b.jpg", "c.jpg"],
         "auto_trash": ["c.jpg"]},
    ])
    rows = dedup.plan_quarantine(root)
    assert len(rows) == 2
    assert len({"a.jpg", "b.jpg", "c.jpg"} - _names(rows)) == 1


def test_missing_members_are_skipped_so_reruns_are_safe(tmp_path):
    root = tmp_path / "lib"
    _touch(root / "a.jpg")
    _write_dupes(root, [{"kind": "exact", "keep": "a.jpg",
                         "auto_trash": ["already-gone.jpg"]}])
    assert dedup.plan_quarantine(root) == []


def test_no_dupes_report_is_not_an_error(tmp_path):
    root = tmp_path / "lib"
    root.mkdir(parents=True)
    assert dedup.plan_quarantine(root) == []


def test_quarantine_round_trips_through_apply_and_undo(tmp_path):
    """End to end: the plan executes and the run is fully reversible."""
    root = tmp_path / "lib"
    _touch(root / "keep.jpg", "original")
    _touch(root / "sub" / "copy.jpg", "original")
    _write_dupes(root, [{"kind": "identical-pixels", "keep": "keep.jpg",
                         "auto_trash": ["sub/copy.jpg"]}])

    rows = dedup.plan_quarantine(root)
    log = tmp_path / "log.csv"
    photos.apply_plan(rows, log, quiet=True)

    assert (root / "keep.jpg").exists(), "the keeper must survive"
    assert not (root / "sub" / "copy.jpg").exists()
    assert (root / ".trash" / "dupes" / "sub" / "copy.jpg").read_text(encoding="utf-8") \
        == "original", "quarantine must preserve the relative path and content"

    photos.undo(log, quiet=True)
    assert (root / "sub" / "copy.jpg").read_text(encoding="utf-8") == "original"
    assert (root / "keep.jpg").exists()


def test_custom_trash_directory_is_honoured(tmp_path):
    root = tmp_path / "lib"
    _touch(root / "a.jpg"), _touch(root / "b.jpg")
    _write_dupes(root, [{"kind": "exact", "keep": "a.jpg", "auto_trash": ["b.jpg"]}])
    rows = dedup.plan_quarantine(root, trash=tmp_path / "elsewhere")
    assert str(tmp_path / "elsewhere") in rows[0]["dst"]
