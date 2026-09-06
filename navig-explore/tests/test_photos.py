"""Tests for navig_explore.photos.

These cover the decisions that can silently destroy a personal photo archive:
which folders are protected from re-dating, that a capture date is never invented
from the filesystem clock, that caption files are not mistaken for litter, and that
apply/undo round-trips without ever overwriting an existing file.
"""
from __future__ import annotations

import os

import pytest

from navig_explore import photos as ph


# ── placement: what automation is forbidden to reshape ──────────────────────
@pytest.mark.parametrize("rel", [
    "By Date/2009/Alzon 2009/IMG_0001.JPG",
    "By Date/2009/14 juillet 2009/x.jpg",
    "By Date/2014/Берлин 2014/x.jpg",
    "By Date/2009/Derniere soirée a l'appart/x.jpg",
    "By Date/2008/Jour de lan 2008/x.jpg",
])
def test_named_event_folders_are_protected(rel):
    """The whole point of this module. These names cannot be rebuilt from metadata."""
    assert ph.placement(rel) == "named"


@pytest.mark.parametrize("rel,expected", [
    ("By Date/2003/05.12.03/x.jpg", "day"),
    ("By Date/2003/27-07-03/x.jpg", "day"),
    ("By Date/2009/2009-06/x.jpg", "bucket"),
    ("By Date/2013/2013-00/x.jpg", "stub"),
    ("By Date/_Undated/file_1.jpg", "loose"),
    ("To Sort/Import/Canon/x.jpg", "loose"),
    ("By Date/2020/miztizm_20200311/x.jpg", "loose"),
])
def test_placement_classification(rel, expected):
    assert ph.placement(rel) == expected


def test_impossible_years_are_not_protected_placements():
    """A future year or epoch-zero is a broken timestamp, not curation."""
    assert ph.placement("By Date/2031/2031-07/x.jpg", max_year=2026) == "loose"
    assert ph.placement("By Date/1970/1970-02/x.jpg") == "loose"
    assert ph.placement("By Date/2009/2009-07/x.jpg") == "bucket"


def test_date_root_is_configurable():
    assert ph.placement("Photos/2009/Alzon 2009/x.jpg", date_root="Photos") == "named"
    # the same path is NOT in the date tree when date_root differs
    assert ph.placement("Photos/2009/Alzon 2009/x.jpg", date_root="By Date") == "loose"


# ── dates: EXIF only ────────────────────────────────────────────────────────
def test_exif_bucket_reads_real_capture_dates():
    assert ph.exif_bucket({"created": "2009:06:14 10:22:31"}) == "2009/2009-06"
    assert ph.exif_bucket({"created": "2024-01-05T13:13:20"}) == "2024/2024-01"


def test_exif_bucket_never_falls_back_to_the_filesystem_clock():
    """Recovered archives carry meaningless mtimes — trusting them files photos
    under a lie, which is worse than leaving them undated."""
    assert ph.exif_bucket({"created": ""}) is None
    assert ph.exif_bucket({"created": "0000:00:00 00:00:00"}) is None
    assert ph.exif_bucket({"created": "", "year": 2010, "mtime": 1.0}) is None


def test_exif_bucket_rejects_future_dates():
    assert ph.exif_bucket({"created": "2031:07:01 00:00:00"}, max_year=2026) is None


@pytest.mark.parametrize("name,expected", [
    ("IMG_20150817_090050.jpg", "2015/2015-08"),
    ("PXL_20220103_120000.jpg", "2022/2022-01"),
    ("2023_01_20_22_30_IMG_7648.JPG", "2023/2023-01"),
    ("IMG_0929.JPG", None),
    ("file_84977.jpg", None),
])
def test_fname_bucket(name, expected):
    assert ph.fname_bucket(name) == expected


# ── junk vs captions ────────────────────────────────────────────────────────
def test_zero_byte_caption_files_are_not_junk():
    """Some folders are described ONLY by an empty file whose name is the note."""
    assert ph.is_junk("By Date/2003/08.06.03/a la plage.txt",
                      "a la plage.txt", ".txt", 0) is None


def test_real_litter_is_detected():
    assert ph.is_junk("Albums/x/Thumbs.db", "Thumbs.db", ".db", 4096)
    assert ph.is_junk("Albums/People/iPod Photo Cache/F01/a.ithmb", "a.ithmb", ".ithmb", 99)
    assert ph.is_junk("By Date/_Undated/broken.jpg", "broken.jpg", ".jpg", 0)


def test_a_normal_photo_is_not_junk():
    assert ph.is_junk("By Date/2009/2009-06/IMG_1.JPG", "IMG_1.JPG", ".jpg", 3_000_000) is None


def test_a_folder_named_thumbnails_is_not_assumed_to_be_cache():
    """Regression: a real library had `_not-personal/Web/Thumbnails` full of images
    the operator deliberately filed there. Matching the NAME would have trashed 82
    wanted photos — cache detection must stick to unambiguous machine folders."""
    assert ph.is_junk("_not-personal/Web/Thumbnails/20210523_153054.jpg",
                      "20210523_153054.jpg", ".jpg", 50_000) is None
    assert ph.is_junk("Albums/People/iPod Photo Cache/F01/a.ithmb",
                      "a.ithmb", ".ithmb", 50_000)


# ── apply / undo round-trip ─────────────────────────────────────────────────
def _touch(p, text="x"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_apply_never_overwrites_and_undo_restores(tmp_path):
    src_dir, dst_dir = tmp_path / "src" / "Named Event 2009", tmp_path / "dst"
    _touch(src_dir / "a.jpg", "alpha")
    _touch(dst_dir / "a.jpg", "occupied")      # destination already taken

    rows = [{"action": "redate", "reason": "t", "size": 5,
             "src": str(src_dir / "a.jpg"), "dst": str(dst_dir / "a.jpg")}]
    log = tmp_path / "log.csv"
    st = ph.apply_plan(rows, log, quiet=True)

    assert st["redate"] == 1
    # the pre-existing file MUST survive untouched
    assert (dst_dir / "a.jpg").read_text(encoding="utf-8") == "occupied"
    assert (dst_dir / "a (1).jpg").read_text(encoding="utf-8") == "alpha"

    ph.undo(log, quiet=True)
    assert (src_dir / "a.jpg").read_text(encoding="utf-8") == "alpha"
    assert not (dst_dir / "a (1).jpg").exists()
    assert (dst_dir / "a.jpg").read_text(encoding="utf-8") == "occupied"


def test_apply_orders_trash_before_redate(tmp_path):
    """A redate whose destination is currently held by a duplicate must land on the
    clean name — so the trash moves that vacate it have to run first."""
    _touch(tmp_path / "dupe.jpg", "dupe")
    _touch(tmp_path / "new.jpg", "keeper")
    rows = [
        {"action": "redate", "reason": "r", "size": 6,
         "src": str(tmp_path / "new.jpg"), "dst": str(tmp_path / "out" / "p.jpg")},
        {"action": "trash-dupe", "reason": "d", "size": 4,
         "src": str(tmp_path / "dupe.jpg"), "dst": str(tmp_path / "out" / "p.jpg")},
    ]
    ph.apply_plan(rows, tmp_path / "log.csv", quiet=True)
    # trash-dupe took the name first, so the keeper is the "(1)" — but crucially both
    # survive and neither was overwritten
    names = sorted(p.name for p in (tmp_path / "out").iterdir())
    assert names == ["p (1).jpg", "p.jpg"]
    assert (tmp_path / "out" / "p.jpg").read_text(encoding="utf-8") == "dupe"


def test_apply_is_idempotent_on_rerun(tmp_path):
    _touch(tmp_path / "a.jpg", "a")
    rows = [{"action": "redate", "reason": "r", "size": 1,
             "src": str(tmp_path / "a.jpg"), "dst": str(tmp_path / "out" / "a.jpg")}]
    ph.apply_plan(rows, tmp_path / "l1.csv", quiet=True)
    st = ph.apply_plan(rows, tmp_path / "l2.csv", quiet=True)
    assert st.get("redate:gone") == 1          # source already moved, not an error
    assert st.get("redate", 0) == 0


def test_prune_empty_dirs_removes_only_empty_ones(tmp_path):
    _touch(tmp_path / "keep" / "a.jpg")
    (tmp_path / "empty" / "deeper").mkdir(parents=True)
    removed = ph.prune_empty_dirs(tmp_path, apply=True)
    assert not (tmp_path / "empty").exists()
    assert (tmp_path / "keep" / "a.jpg").exists()
    assert any("empty" in r for r in removed)


# ── end-to-end plan over a realistic fixture ────────────────────────────────
def test_build_plan_protects_curation_and_files_the_rest(tmp_path):
    root = tmp_path / "Photos"
    # a named event folder — must be untouched
    _touch(root / "By Date" / "2009" / "Alzon 2009" / "IMG_1.JPG", "photo")
    # a caption — must be untouched
    _touch(root / "By Date" / "2003" / "05.12.03" / "a la plage.txt", "")
    # litter — must be quarantined
    _touch(root / "Albums" / "Thumbs.db", "junk")
    # recovery salvage in the unsorted pile — must be quarantined
    _touch(root / "To Sort" / "file_84977.jpg", "salvage")
    # a datable filename in the unsorted pile — must be filed
    _touch(root / "To Sort" / "IMG_20150817_090050.jpg", "dated")
    (root / ".mediaexplorer").mkdir(parents=True, exist_ok=True)
    (root / ".mediaexplorer" / "meta.jsonl").write_text("", encoding="utf-8")

    rows, summary = ph.build_plan(root, loose={"to sort"})
    by_action = {}
    for r in rows:
        by_action.setdefault(r["action"], []).append(os.path.basename(r["src"]))

    assert "IMG_1.JPG" not in str(rows), "a named event folder was touched"
    assert "a la plage.txt" not in str(rows), "a caption file was touched"
    assert by_action.get("trash-junk") == ["Thumbs.db"]
    assert by_action.get("quarantine") == ["file_84977.jpg"]
    assert by_action.get("redate") == ["IMG_20150817_090050.jpg"]
    assert summary["keep-named"] == 1


def test_tiny_images_in_curated_albums_are_left_alone(tmp_path):
    """Regression: gating the size filter on 'not in the date tree' swept 2,546 small
    images out of hand-made albums. A curated collection's small image was kept on
    purpose; only the unsorted piles get filtered by size."""
    root = tmp_path / "Photos"
    _touch(root / "Albums" / "Trips" / "tiny.jpg", "x")            # curated -> keep
    _touch(root / "To Sort" / "tiny.jpg", "x")                     # unsorted -> sweep
    (root / ".mediaexplorer").mkdir(parents=True, exist_ok=True)
    meta = [{"rel": "Albums/Trips/tiny.jpg", "type": "image"},
            {"rel": "To Sort/tiny.jpg", "type": "image"}]
    (root / ".mediaexplorer" / "meta.jsonl").write_text(
        "\n".join(__import__("json").dumps(m) for m in meta), encoding="utf-8")

    rows, summary = ph.build_plan(root, loose={"to sort"})
    swept = [r["src"] for r in rows if r["action"] == "trash-tiny"]
    assert len(swept) == 1
    assert "To Sort" in swept[0]
    assert summary["keep-curated"] >= 1
