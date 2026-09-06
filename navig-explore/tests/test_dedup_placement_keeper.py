"""The keeper must be chosen by what a placement MEANS, not by file size.

Measured on a real 121k-photo library: with size deciding (which is what happens once
equal pixels tie every other term), 10,713 photos would have been removed from
human-named event and day folders while the surviving copies sat in date buckets that
EXIF can rebuild for free. That is the most valuable organisation in the library being
traded for a few KB of metadata.
"""
from __future__ import annotations

import pytest

from navig_explore import dedup, photos


def _rec(rel, size=1000, w=100, h=100):
    return {"rel": rel, "name": rel.split("/")[-1], "size": size, "w": w, "h": h,
            "_abs": "X:/" + rel}


def rank(rel):
    return photos.placement_rank(rel)


# ── the ranking itself ──────────────────────────────────────────────────────
@pytest.mark.parametrize("rel,kind", [
    ("By Date/2009/Alzon 2009/IMG_1.JPG", "named"),
    ("By Date/2003/05.12.03/IMG_1.JPG", "day"),
    ("Albums/Trips/IMG_1.JPG", "curated"),
    ("Archives/ed/IMG_1.JPG", "curated"),
    ("Camera/100CANON/IMG_1.JPG", "curated"),
    ("By Date/2009/2009-06/IMG_1.JPG", "bucket"),
    ("By Date/2013/2013-00/IMG_1.JPG", "stub"),
    ("By Date/_Undated/IMG_1.JPG", "undated"),
    ("To Sort/Import/IMG_1.JPG", "staging"),
])
def test_placement_kind(rel, kind):
    assert photos.placement_kind(rel) == kind


def test_rank_order_is_named_day_curated_bucket_stub_undated_staging():
    order = ["By Date/2009/Alzon 2009/a.jpg", "By Date/2003/05.12.03/a.jpg",
             "Albums/Trips/a.jpg", "By Date/2009/2009-06/a.jpg",
             "By Date/2013/2013-00/a.jpg", "By Date/_Undated/a.jpg",
             "To Sort/a.jpg"]
    ranks = [photos.placement_rank(r) for r in order]
    assert ranks == sorted(ranks, reverse=True), ranks
    assert len(set(ranks)) == len(ranks), "each placement must be distinguishable"


# ── the keeper decision ─────────────────────────────────────────────────────
def test_named_event_copy_beats_a_LARGER_date_bucket_copy():
    """The exact regression: size alone kept the bucket copy and dropped the event."""
    named = _rec("By Date/2009/Alzon 2009/IMG_1.JPG", size=1000)
    bucket = _rec("By Date/2009/2009-07/IMG_1.JPG", size=9999)   # more metadata
    assert dedup._pick_keep([named, bucket])["rel"] == bucket["rel"], \
        "baseline: without a rank, the bigger file wins"
    assert dedup._pick_keep([named, bucket], rank)["rel"] == named["rel"], \
        "with placement ranking, the named event folder must win"


def test_day_folder_beats_undated_even_when_smaller():
    day = _rec("By Date/2003/05.12.03/a.jpg", size=10)
    undated = _rec("By Date/_Undated/a.jpg", size=5_000_000)
    assert dedup._pick_keep([day, undated], rank)["rel"] == day["rel"]


def test_curated_album_beats_staging():
    album = _rec("Albums/Trips/a.jpg", size=10)
    staging = _rec("To Sort/Import/a.jpg", size=5_000_000)
    assert dedup._pick_keep([album, staging], rank)["rel"] == album["rel"]


def test_within_the_same_placement_size_still_decides():
    """Ranking is a tie-breaker ABOVE the intrinsic score, not a replacement for it."""
    small = _rec("By Date/2009/Alzon 2009/a.jpg", size=100)
    big = _rec("By Date/2009/Alzon 2009/b.jpg", size=900)
    assert dedup._pick_keep([small, big], rank)["rel"] == big["rel"]


def test_higher_resolution_still_wins_within_a_placement():
    lo = _rec("Albums/Trips/a.jpg", size=9_000_000, w=100, h=100)
    hi = _rec("Albums/Trips/b.jpg", size=10, w=4000, h=3000)
    assert dedup._pick_keep([lo, hi], rank)["rel"] == hi["rel"]


def test_ranking_is_opt_in_and_default_behaviour_is_unchanged():
    named = _rec("By Date/2009/Alzon 2009/a.jpg", size=1)
    bucket = _rec("By Date/2009/2009-07/a.jpg", size=2)
    assert dedup._pick_keep([named, bucket])["rel"] == bucket["rel"]


def test_end_to_end_group_keeps_the_named_copy(tmp_path):
    """A whole find_exact group, not just the picker."""
    import hashlib
    root = tmp_path
    named_p = root / "By Date" / "2009" / "Alzon 2009" / "IMG_1.JPG"
    bucket_p = root / "By Date" / "2009" / "2009-07" / "IMG_1.JPG"
    for p in (named_p, bucket_p):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"identical bytes")
    recs = []
    for p in (named_p, bucket_p):
        rel = str(p.relative_to(root)).replace("\\", "/")
        recs.append({"rel": rel, "name": p.name, "size": p.stat().st_size,
                     "_abs": str(p), "w": 10, "h": 10})
    groups = dedup.find_exact(recs, lambda *a: None, workers=2, rank=rank)
    assert len(groups) == 1
    assert groups[0]["keep"].endswith("Alzon 2009/IMG_1.JPG")
    assert groups[0]["auto_trash"] == ["By Date/2009/2009-07/IMG_1.JPG"]
    assert hashlib.sha256  # keep the import meaningful for readers
