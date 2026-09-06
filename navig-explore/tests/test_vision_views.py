"""The organised view tree — and the pollution that made the first attempt useless.

The original `by-year` held 51,459 entries of which only 27,767 were photographs;
webcam frames, screenshots, web graphics and documents were mixed into the same
year folders. Every iPhone photo was present and none of them findable. These
tests exist so a view cannot silently fill up with things that are not photos.
"""
from __future__ import annotations

import json

import pytest

from navig_explore.vision import arrange, catalog, views


def _write_meta(root, rows):
    side = root / ".mediaexplorer"
    side.mkdir(parents=True, exist_ok=True)
    with (side / "meta.jsonl").open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _lib(tmp_path, files):
    """files: [(rel, sha, klass, date|None, camera)]"""
    root = tmp_path / "lib"
    root.mkdir()
    _write_meta(root, [
        {"rel": rel, "abs": str(root / rel), "name": rel.split("/")[-1], "ext": ".jpg",
         "type": "image", "size": 10, "mtime": 0.0, "probe_ok": True, "w": 800,
         "h": 600, "created": "", "camera": cam, "gps": None, "res_tier": "SD",
         "source_class": "x"}
        for rel, _sha, _k, _d, cam in files])
    conn = catalog.connect(root)
    catalog.ingest_meta_jsonl(conn, root, quiet=True)
    for rel, sha, klass, date, _cam in files:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"px-" + sha.encode())
        conn.execute("UPDATE files SET sha256=? WHERE rel=?", (sha, rel))
        conn.execute("INSERT OR IGNORE INTO assets (sha256, decoded) VALUES (?,1)", (sha,))
        conn.execute("INSERT OR REPLACE INTO classes (sha256, class, score) VALUES (?,?,1.0)",
                     (sha, klass))
        if date:
            conn.execute("""INSERT OR REPLACE INTO dates (sha256, value, source, confidence)
                            VALUES (?,?,'exif',1.0)""", (sha, date))
    conn.commit()
    return root, conn


# ── the pollution regression ────────────────────────────────────────────────
def test_year_view_holds_photographs_only(tmp_path):
    """Regression: by-year was 46% webcam frames, screenshots and web graphics."""
    root, _ = _lib(tmp_path, [
        ("a.jpg", "a" * 64, "photo", "2019-06-01T12:00:00", ""),
        ("b.jpg", "b" * 64, "screenshot", "2019-06-02T12:00:00", ""),
        ("c.jpg", "c" * 64, "webcam", "2019-06-03T12:00:00", ""),
        ("d.jpg", "d" * 64, "web-graphic", "2019-06-04T12:00:00", ""),
    ])
    rows, summary = arrange.build_plan(root, tmp_path / "v", "year",
                                       only_classes=("photo",))
    assert summary == {"2019": 1}
    assert [r["src"].endswith("a.jpg") for r in rows] == [True]


def test_every_photo_facing_view_is_filtered(tmp_path):
    """The whole tree, not just by-year — nothing non-photo may leak in."""
    root, _ = _lib(tmp_path, [
        ("By Date/2019/Trip/a.jpg", "a" * 64, "photo", "2019-06-01T12:00:00", ""),
        ("By Date/2019/Trip/b.jpg", "b" * 64, "screenshot", "2019-06-02T12:00:00", ""),
    ])
    rows, _s = views.build_plan(root, tmp_path / "v", min_group=1,
                                min_distinct_photos=1)
    photo_views = {"by-year", "by-event", "by-person", "by-place"}
    leaked = [r for r in rows if r["view"] in photo_views and r["src"].endswith("b.jpg")]
    assert leaked == [], "a non-photograph reached a photo-facing view"


def test_non_photos_still_get_their_own_bucket(tmp_path):
    """Filtering them out of by-year must not make them disappear entirely.

    A file with no embedding cannot be sub-typed, and must land in `other`
    rather than vanishing from a view that claims to hold every screenshot.
    """
    root, _ = _lib(tmp_path, [
        ("s.jpg", "s" * 64, "screenshot", "2019-06-02T12:00:00", "Apple iPhone 13 Pro"),
        ("w.jpg", "w" * 64, "webcam", "2019-06-03T12:00:00", ""),
    ])
    rows, summary = views.build_plan(root, tmp_path / "v", min_group=1,
                                     min_distinct_photos=1)
    assert summary["screenshots"]["links"] == 1
    assert summary["webcam"]["links"] == 1
    assert any(r["view"] == "screenshots" and r["bucket"] == "other" for r in rows)


def test_only_a_wholly_unreadable_occasion_counts_as_lost(tmp_path):
    """Regression: the file said 51 lost occasions, `photos events` said 20.

    Filtering rows to the undecodable ones and *then* grouping lists any event
    holding a single damaged file. A partly damaged occasion still has a folder
    and is not lost — and two numbers describing the same thing must not differ.
    """
    from navig_explore.vision import events as EV

    root, conn = _lib(tmp_path, [
        # every file unreadable — genuinely lost
        ("By Date/2005/All gone/a.jpg", "a" * 64, "photo", None, ""),
        ("By Date/2005/All gone/b.jpg", "b" * 64, "photo", None, ""),
        # one bad file among good ones — still has a folder
        ("By Date/2005/Mostly fine/c.jpg", "c" * 64, "photo", None, ""),
        ("By Date/2005/Mostly fine/d.jpg", "d" * 64, "photo", None, ""),
    ])
    conn.execute("UPDATE assets SET decoded = 0 WHERE sha256 IN (?,?,?)",
                 ("a" * 64, "b" * 64, "c" * 64))
    conn.commit()
    EV.detect(root, quiet=True)

    lost = views.lost_occasions(conn)
    assert len(lost) == 1, [r["event_name"] for r in lost]
    assert "All gone" in lost[0]["event_name"]
    assert lost[0]["n"] == 2, "the count is the whole occasion, not its damaged part"
    assert len(lost) == EV.detect(root, quiet=True)["unviewable_events"]


def test_screenshots_are_filed_by_what_they_show_not_which_phone(tmp_path):
    """11,469 of 15,130 captures came from one iPhone.

    Splitting by device therefore produced one enormous folder called `iPhone`
    with nothing findable inside it — an answer to a question nobody asks. The
    device facet still exists and is still exact; it is just not the view.
    """
    assert dict(views.VIEWS and
                {name: facet for name, facet, _kw in views.VIEWS})["screenshots"] \
        == "screenshot-type"
    assert "screenshot-source" in arrange.FACETS, "the device split is still available"


def test_every_screenshot_type_is_a_usable_folder_name():
    from navig_explore.vision import classify

    for t in classify.SHOT_TYPES:
        assert t == t.strip() and "/" not in t and "\\" not in t
    assert "other" not in classify.SHOT_TYPES, (
        "`other` is the honest fallback and must not also be a prompt set")


# ── the event facet ─────────────────────────────────────────────────────────
def test_event_facet_uses_named_folders_not_date_buckets(tmp_path):
    """`By Date/2009/2009-02` is a date bucket; `Alzon 2009` is an event.

    The facet now reads the `events` table, so detection runs first — that is
    what lets an occasion the operator never named be reachable too.
    """
    from navig_explore.vision import events as EV

    root, _ = _lib(tmp_path, [
        ("By Date/2009/Alzon 2009/a.jpg", "a" * 64, "photo", None, ""),
        ("By Date/2009/2009-02/b.jpg", "b" * 64, "photo", None, ""),
    ])
    EV.detect(root, quiet=True)
    _rows, summary = arrange.build_plan(root, tmp_path / "v", "event",
                                        only_classes=("photo",))
    # `Alzon 2009` inside `by-event/2009/` loses the year it is already filed by.
    assert list(summary) == ["2009/Alzon"], summary


def test_event_facet_keeps_non_ascii_names(tmp_path):
    from navig_explore.vision import events as EV

    root, _ = _lib(tmp_path, [
        ("By Date/2014/Берлин 2014/a.jpg", "a" * 64, "photo", None, ""),
    ])
    EV.detect(root, quiet=True)
    _rows, summary = arrange.build_plan(root, tmp_path / "v", "event",
                                        only_classes=("photo",))
    assert "2014/Берлин" in summary, summary


# ── screenshot sources ──────────────────────────────────────────────────────
@pytest.mark.parametrize(("camera", "bucket"), [
    ("Apple iPhone 13 Pro", "iPhone"),
    ("Apple iPad Pro", "iPad"),
    ("Canon EOS 200D", "other"),
    ("", "other"),
])
def test_screenshot_source_falls_back_to_camera_then_other(tmp_path, camera, bucket):
    """With an unremarkable resolution, the camera tag is all that is left."""
    root, _ = _lib(tmp_path, [("s.jpg", "s" * 64, "screenshot", None, camera)])
    _rows, summary = arrange.build_plan(root, tmp_path / "v", "screenshot-source",
                                        only_classes=("screenshot",))
    assert bucket in summary


@pytest.mark.parametrize(("w", "h", "bucket"), [
    (1170, 2532, "iPhone"),        # iPhone 13/14 Pro
    (2532, 1170, "iPhone"),        # the same, rotated
    (1536, 2048, "iPad"),
    (1080, 2340, "other phone"),
    (2560, 1440, "desktop"),       # landscape monitor…
    (1440, 2560, "other phone"),   # …the same numbers, portrait: a handset
])
def test_screenshot_device_comes_from_the_screen_size(tmp_path, w, h, bucket):
    """A screenshot has no camera EXIF by definition — the resolution IS the device.

    Orientation has to be checked first: 2560x1440 is a monitor, 1440x2560 is a
    phone, and matching both orientations put desktop captures under Android.
    """
    from navig_explore.vision.classify_rules import device_for
    assert device_for(w, h) == bucket


# ── person-group quality ────────────────────────────────────────────────────
def test_a_group_from_one_repeated_image_is_not_a_person(tmp_path):
    """The folders that turned out to hold a mask and a cartoon emoji.

    Many faces, one source image — that is artwork or a repeated frame.
    """
    root, conn = _lib(tmp_path, [("a.jpg", "a" * 64, "photo", None, "")])
    conn.execute("INSERT INTO people (person_id, name, locked) VALUES (1,NULL,0)")
    for i in range(40):                       # 40 faces, all from ONE photo
        conn.execute("""INSERT INTO faces (sha256,x,y,w,h,score,rotation,engine,person_id)
                        VALUES (?,0,0,.1,.1,.9,0,'t',1)""", ("a" * 64,))
    conn.commit()

    _rows, summary = arrange.build_plan(
        root, tmp_path / "v", "person", include_unnamed=True,
        min_group=30, min_distinct_photos=10, only_classes=("photo",))
    assert summary == {}, "an artwork group was published as a person"


def test_a_real_person_across_many_photos_survives(tmp_path):
    root, conn = _lib(tmp_path, [
        (f"p{i}.jpg", f"{i:064d}", "photo", None, "") for i in range(12)])
    conn.execute("INSERT INTO people (person_id, name, locked) VALUES (1,'Anna',1)")
    for i in range(12):
        conn.execute("""INSERT INTO faces (sha256,x,y,w,h,score,rotation,engine,person_id)
                        VALUES (?,0,0,.1,.1,.9,0,'t',1)""", (f"{i:064d}",))
    conn.commit()

    _rows, summary = arrange.build_plan(
        root, tmp_path / "v", "person", include_unnamed=True,
        min_group=10, min_distinct_photos=10, only_classes=("photo",))
    assert summary == {"Anna": 12}


# ── the destination rule ────────────────────────────────────────────────────
def test_organized_is_allowed_inside_the_library(tmp_path):
    """`_organized` is in every scanner's skip list, so a tree there is safe."""
    root, _ = _lib(tmp_path, [("a.jpg", "a" * 64, "photo", "2019-01-01T00:00:00", "")])
    rows, _s = arrange.build_plan(root, root / "_organized" / "by-year", "year",
                                  only_classes=("photo",))
    assert rows


def test_an_unprotected_folder_inside_the_library_is_still_refused(tmp_path):
    root, _ = _lib(tmp_path, [("a.jpg", "a" * 64, "photo", "2019-01-01T00:00:00", "")])
    with pytest.raises(arrange.DestinationInsideLibrary):
        arrange.build_plan(root, root / "my-views", "year", only_classes=("photo",))


def test_default_destination_is_the_safe_one(tmp_path):
    assert views.default_dest(tmp_path).name == "_organized"


def test_coverage_reports_the_gap_rather_than_hiding_it(tmp_path):
    """A year view cannot show weakly-dated photos; the number must be stated."""
    root, conn = _lib(tmp_path, [
        ("a.jpg", "a" * 64, "photo", "2019-01-01T00:00:00", ""),
        ("b.jpg", "b" * 64, "photo", "2011-01-01T00:00:00", ""),
    ])
    conn.execute("UPDATE dates SET confidence=0.60 WHERE sha256=?", ("b" * 64,))
    conn.commit()
    cov = views.coverage(conn, root, arrange.MIN_DATE_CONFIDENCE)
    assert cov == {"dated": 2, "confident": 1}
