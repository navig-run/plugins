"""Occasions — curated names are sacred, the rest is inferred from timestamps.

This library is curated in half: 27-81 named event folders a year across
2003-2010, then almost none after. So the module has to do two different things
at once, and the tests here mostly guard the seam between them — a hand-made
name must never be rewritten, re-clustered, or filtered away.
"""
from __future__ import annotations

from datetime import datetime as D

import pytest

from navig_explore.vision import catalog, events as EV


# ── naming ──────────────────────────────────────────────────────────────────
def test_a_curated_name_is_prefixed_never_rewritten():
    assert EV.label_for_curated("Jour de lan 2008", D(2008, 1, 1)) == \
        "2008-01-01 · Jour de lan 2008"


@pytest.mark.parametrize(("folder", "year", "want"), [
    # The operator writes dates four different ways. One year folder held
    # `01-08-07`, `06.04.07 - chez adrien GIGNAC`, `2007.03.03_Aniif d'otomne`,
    # `alzon 2007` and `2007-01-09 (3)` side by side and sorted by none of them.
    ("2008.08.10_teuf marseille", "2008", "2008-08-10 · Teuf marseille"),
    ("06.04.07 - chez adrien GIGNAC", "2007", "2007-04-06 · Chez adrien GIGNAC"),
    ("02.07.2005 - fete de la musique 2005", "2005",
     "2005-07-02 · Fete de la musique"),
    ("perthus 02-08-07", "2007", "2007-08-02 · Perthus"),
    # A name that is only a date becomes the prefix; there is nothing else to keep.
    ("04.10.03", "2003", "2003-10-04"),
])
def test_a_date_written_into_the_folder_name_is_used_and_normalised(folder, year, want):
    """Half the curated folders carry their own date, and it beats EXIF.

    266 of 336 named events are salvage with no readable timestamp at all, so
    before this the year folder simply did not sort.
    """
    assert EV.label_for_curated(folder, None, year) == want


def test_a_written_date_from_the_wrong_year_is_not_believed():
    """`06.04.07` under `By Date/2011/` is a phone number, not April 2007."""
    assert EV.label_for_curated("truc 06.04.07", None, "2011") == "Truc 06.04.07"


def test_a_non_ascii_curated_name_survives():
    assert EV.label_for_curated("Берлин 2014", D(2014, 5, 3)) == "2014-05-03 · Берлин 2014"


def test_a_curated_name_without_any_date_keeps_its_words():
    """Only the first letter is ever touched, and only upward."""
    assert EV.label_for_curated("airsoft", None) == "Airsoft"
    assert EV.label_for_curated("chez adrien GIGNAC", None) == "Chez adrien GIGNAC"


def test_a_year_the_folder_already_states_is_not_repeated():
    """`by-event/2007/2007-07-15 · Alzon 2007` says 2007 three times."""
    assert EV.label_for_curated("alzon 2007", D(2007, 7, 15), "2007") == \
        "2007-07-15 · Alzon"
    # …including when there is no date at all, so a dated and an undated name
    # in the same year folder read as the same kind of thing.
    assert EV.label_for_curated("Noel 2007", None, "2007") == "Noel"
    assert EV.label_for_curated("Web cam 2007", None, "2007") == "Web cam"
    # …but never to nothing.
    assert EV.label_for_curated("2007", D(2007, 7, 15), "2007") == "2007-07-15"
    assert EV.label_for_curated("2007", None, "2007") == "2007"


def test_a_year_that_is_NOT_the_folder_year_is_kept():
    """`Retro 1998` filed under 2009 is a name, not a redundancy."""
    assert EV.label_for_curated("Retro 1998", None, "2009") == "Retro 1998"


def test_a_date_that_contradicts_the_folder_year_is_refused():
    """Regression: `2018-05-09 · Jour de lan 2008`, filed under 2008.

    Photographs in that folder carry dates as late as 2018 — rescans, or a date
    inherited from the wrong near-duplicate. The operator's own folder is better
    evidence of when than anything derived from its contents.
    """
    assert EV.label_for_curated("Jour de lan 2008", D(2018, 5, 9), "2008") == \
        "Jour de lan", "2018 must not reach the name of a 2008 occasion"
    assert EV.label_for_curated("camping ray charles", D(2024, 8, 11), "2008") == \
        "Camping ray charles"


def test_a_date_that_agrees_with_the_folder_year_is_used():
    assert EV.label_for_curated("Alzon", D(2009, 7, 20), "2009") == \
        "2009-07-20 · Alzon"


@pytest.mark.parametrize(("dates", "hint", "want"), [
    ([D(2019, 7, 12, 9), D(2019, 7, 12, 20)], "Montpellier, FR",
     "2019-07-12 · Montpellier, FR"),
    ([D(2019, 7, 12), D(2019, 7, 14)], "Sète, FR", "2019-07-12..14 · Sète, FR"),
    ([D(2019, 7, 30), D(2019, 8, 2)], "", "2019-07-30..08-02"),
    ([D(2021, 5, 3), D(2021, 5, 3)], "", "2021-05-03"),
])
def test_detected_names_lead_with_the_date(dates, hint, want):
    """Only the date is measured, so it goes first; the hint may be absent.

    No photo count: `2007-01-09 (3)` reads as a number stuck onto a name, and
    the folder shows its own size the moment it is opened.
    """
    assert EV.label_for_detected(dates, hint) == want


# ── dating a curated folder ─────────────────────────────────────────────────
def test_a_weak_date_inside_the_right_year_can_still_order_a_curated_folder():
    """The parent folder corroborates the year, so a weak date is enough to sort by.

    Demanding confidence >= 0.80 for the prefix left 266 of 336 named events
    undated — they are salvage, and their EXIF is gone.
    """
    assert EV.resolve_curated_date([], [D(2003, 5, 4), D(2003, 5, 6)], "2003") == \
        D(2003, 5, 6)


def test_a_confident_date_beats_a_weak_one():
    assert EV.resolve_curated_date([D(2003, 1, 9)], [D(2003, 11, 2)], "2003") == \
        D(2003, 1, 9)


def test_dates_from_outside_the_folder_year_are_ignored_entirely():
    assert EV.resolve_curated_date([D(2018, 5, 9)], [D(2024, 1, 1)], "2008") is None


def test_a_year_only_date_cannot_prefix_a_curated_folder(tmp_path):
    """Regression: fourteen 2007 occasions all filed as `2007-07-01`.

    `Airsoft`, `Alzon`, `Noel`, `Jour de lan` are salvage with no EXIF; their
    only date came from the parent year folder, stored as noon on 1 July. It
    reads exactly like a measured date and is not one.
    """
    root, conn = _lib(tmp_path, [
        (f"By Date/2007/Airsoft/p{i}.jpg", f"{i:064d}", "photo",
         "2007-07-01T12:00:00", 0.60) for i in range(4)])
    conn.execute("UPDATE dates SET source='folder'")
    conn.commit()
    EV.detect(root, quiet=True)
    name = conn.execute("SELECT DISTINCT event_name FROM events").fetchone()[0]
    assert name == "Airsoft", f"a fabricated day reached the folder name: {name}"


def test_a_device_folder_never_names_an_occasion(tmp_path):
    """59 detected events were called `Canon Powershot A95`."""
    models = {"canon powershot a95", "nikon e2100"}
    assert EV._is_camera_model("Canon Powershot A95", models)
    assert EV._is_camera_model("NIKON E2100", models), "matching is case-folded"
    # The album folder is `Canon Powershot A2000`; the EXIF model is the longer
    # `Canon Powershot A2000 IS`, so containment has to work in both directions.
    assert EV._is_camera_model("Canon Powershot A95 IS", models)
    assert not EV._is_camera_model("Alzon 2009", models)
    assert not EV._is_camera_model("Malaga", models)


@pytest.mark.parametrize("leaf", ["Photos", "photos", "images", "107 06", "Divers",
                                  "New Folder", "DCIM", "100CANON"])
def test_a_container_folder_never_names_an_occasion(leaf):
    assert EV._NOT_AN_OCCASION.match(leaf), leaf


@pytest.mark.parametrize("leaf", ["Malaga", "Alzon 2009", "Берлин", "Chez le russe"])
def test_a_real_place_name_still_names_an_occasion(leaf):
    assert not EV._NOT_AN_OCCASION.match(leaf), leaf


def test_two_occasions_in_a_year_never_share_a_folder_name():
    """Dropping the photo count made collisions possible for the first time."""
    out = EV._disambiguate([
        ("a", "2019", "2019/k1", "2019-05-04", "detected"),
        ("b", "2019", "2019/k2", "2019-05-04", "folder"),
        ("c", "2019", "2019/k3", "2019-06-01", "detected"),
        ("d", "2020", "2020/k4", "2019-05-04", "detected"),
    ])
    names = {key: name for _s, _y, key, name, _src in out}
    assert names["2019/k1"] != names["2019/k2"]
    assert names["2019/k3"] == "2019-06-01", "an unambiguous name gained a suffix"
    assert names["2020/k4"] == "2019-05-04", "a different year is not a collision"


# ── the seam ────────────────────────────────────────────────────────────────
def _lib(tmp_path, files):
    """files: [(rel, sha, klass, iso_date|None, confidence)]"""
    import json

    root = tmp_path / "lib"
    (root / ".mediaexplorer").mkdir(parents=True)
    with (root / ".mediaexplorer" / "meta.jsonl").open("w", encoding="utf-8") as fh:
        for rel, *_ in files:
            fh.write(json.dumps({
                "rel": rel, "abs": str(root / rel), "name": rel.split("/")[-1],
                "ext": ".jpg", "type": "image", "size": 10, "mtime": 0.0,
                "probe_ok": True, "w": 800, "h": 600, "created": "", "camera": "",
                "gps": None, "res_tier": "SD", "source_class": "x"}) + "\n")
    conn = catalog.connect(root)
    catalog.ingest_meta_jsonl(conn, root, quiet=True)
    for rel, sha, klass, iso, conf in files:
        conn.execute("UPDATE files SET sha256=? WHERE rel=?", (sha, rel))
        conn.execute("INSERT OR IGNORE INTO assets (sha256, decoded) VALUES (?,1)", (sha,))
        conn.execute("INSERT OR REPLACE INTO classes (sha256,class,score) VALUES (?,?,1.0)",
                     (sha, klass))
        if iso:
            conn.execute("""INSERT OR REPLACE INTO dates (sha256,value,source,confidence)
                            VALUES (?,?,'exif',?)""", (sha, iso, conf))
    conn.commit()
    return root, conn


def test_a_curated_folder_of_webcam_frames_is_still_an_event(tmp_path):
    """Regression: filtering to `photo` first silently dropped 40 curated events.

    Their contents were webcam frames and salvage — 424 viewable files among
    them. A folder a human named is an occasion whatever is inside it.
    """
    root, conn = _lib(tmp_path, [
        (f"By Date/2004/Chez le russe/{i}.jpg", f"{i:064d}", "webcam", None, 0)
        for i in range(5)])
    EV.detect(root, quiet=True)
    got = conn.execute("SELECT DISTINCT event_name, source FROM events").fetchone()
    assert got is not None, "a curated event was dropped for its file class"
    assert got["source"] == "folder"
    assert "Chez le russe" in got["event_name"]


def test_an_occasion_with_nothing_readable_left_is_counted_not_hidden(tmp_path):
    """62 folders here are 100% corrupt — the name is all that survived.

    The `event` facet only links files that decode, so those occasions get no
    folder. That is right for a view, but it must be said: otherwise the only
    way to learn 20 named afternoons are missing is to diff the catalog against
    the disk, which is how this was found.
    """
    root, conn = _lib(tmp_path, [
        (f"By Date/2005/Chez grandpere thomas 2/{i}.jpg", f"{i:064d}", "photo", None, 0)
        for i in range(4)])
    conn.execute("UPDATE assets SET decoded = 0")
    conn.commit()
    stats = EV.detect(root, quiet=True)
    assert stats["curated_events"] == 1, "the occasion is still in the catalog"
    assert stats["unviewable_events"] == 1


def test_an_occasion_with_one_readable_file_is_viewable(tmp_path):
    root, conn = _lib(tmp_path, [
        (f"By Date/2005/Chez grandpere/{i}.jpg", f"{i:064d}", "photo", None, 0)
        for i in range(4)])
    conn.execute("UPDATE assets SET decoded = 0 WHERE sha256 <> ?", (f"{0:064d}",))
    conn.commit()
    assert EV.detect(root, quiet=True)["unviewable_events"] == 0


def test_detection_only_groups_photographs(tmp_path):
    """Clustering screenshots by timestamp would invent occasions nobody attended."""
    root, conn = _lib(tmp_path, [
        (f"By Date/2019/2019-05/s{i}.jpg", f"{i:064d}", "screenshot",
         f"2019-05-01T10:0{i}:00", 1.0) for i in range(5)])
    EV.detect(root, quiet=True)
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_a_date_bucket_is_not_treated_as_a_curated_name(tmp_path):
    root, conn = _lib(tmp_path, [
        (f"By Date/2019/2019-05/p{i}.jpg", f"{i:064d}", "photo",
         f"2019-05-01T10:0{i}:00", 1.0) for i in range(4)])
    EV.detect(root, quiet=True)
    sources = {r[0] for r in conn.execute("SELECT DISTINCT source FROM events")}
    assert sources == {"detected"}


# ── the gap ─────────────────────────────────────────────────────────────────
def test_a_gap_at_the_threshold_splits_and_one_below_it_does_not(tmp_path):
    root, conn = _lib(tmp_path, [
        ("By Date/2019/2019-05/a1.jpg", "a" * 64, "photo", "2019-05-01T09:00:00", 1.0),
        ("By Date/2019/2019-05/a2.jpg", "b" * 64, "photo", "2019-05-01T12:00:00", 1.0),
        ("By Date/2019/2019-05/a3.jpg", "c" * 64, "photo", "2019-05-01T20:00:00", 1.0),
        # 25 h later — a new occasion
        ("By Date/2019/2019-05/b1.jpg", "d" * 64, "photo", "2019-05-02T21:00:00", 1.0),
        ("By Date/2019/2019-05/b2.jpg", "e" * 64, "photo", "2019-05-02T22:00:00", 1.0),
        ("By Date/2019/2019-05/b3.jpg", "f" * 64, "photo", "2019-05-02T23:00:00", 1.0),
    ])
    EV.detect(root, gap_hours=24, min_size=3, quiet=True)
    keys = {r[0] for r in conn.execute("SELECT DISTINCT event_key FROM events")}
    assert len(keys) == 2, f"expected two occasions, got {keys}"


def test_runs_shorter_than_min_size_get_no_event(tmp_path):
    root, conn = _lib(tmp_path, [
        ("By Date/2019/2019-05/x.jpg", "a" * 64, "photo", "2019-05-01T09:00:00", 1.0),
        ("By Date/2019/2019-05/y.jpg", "b" * 64, "photo", "2019-05-09T09:00:00", 1.0),
    ])
    stats = EV.detect(root, min_size=3, quiet=True)
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert stats["too_small"] == 2


def test_a_weak_date_cannot_place_a_photo_in_time(tmp_path):
    """A folder-year guess must not invent an occasion."""
    root, conn = _lib(tmp_path, [
        (f"By Date/2019/2019-05/p{i}.jpg", f"{i:064d}", "photo",
         "2019-07-01T12:00:00", 0.60) for i in range(4)])
    stats = EV.detect(root, quiet=True)
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert stats["undated"] == 4


def test_every_photo_lands_in_at_most_one_event(tmp_path):
    root, conn = _lib(tmp_path, [
        ("By Date/2009/Alzon 2009/a.jpg", "a" * 64, "photo", "2009-07-20T10:00:00", 1.0),
        ("By Date/2019/2019-05/b.jpg", "b" * 64, "photo", "2019-05-01T09:00:00", 1.0),
        ("By Date/2019/2019-05/c.jpg", "c" * 64, "photo", "2019-05-01T10:00:00", 1.0),
        ("By Date/2019/2019-05/d.jpg", "d" * 64, "photo", "2019-05-01T11:00:00", 1.0),
    ])
    EV.detect(root, quiet=True)
    dupes = conn.execute(
        "SELECT sha256, COUNT(*) n FROM events GROUP BY sha256 HAVING n > 1").fetchall()
    assert dupes == []


def test_a_curated_photo_is_never_reclustered(tmp_path):
    """Even with a perfect date, curation wins over the timestamp."""
    root, conn = _lib(tmp_path, [
        (f"By Date/2009/Alzon 2009/p{i}.jpg", f"{i:064d}", "photo",
         f"2009-07-2{i}T10:00:00", 1.0) for i in range(4)])
    EV.detect(root, gap_hours=1, min_size=1, quiet=True)
    sources = {r[0] for r in conn.execute("SELECT DISTINCT source FROM events")}
    assert sources == {"folder"}
    assert conn.execute("SELECT COUNT(DISTINCT event_key) FROM events").fetchone()[0] == 1


def test_event_labels_cannot_escape_the_destination():
    from pathlib import Path

    from navig_explore.vision.arrange import _bucket_dir

    dest = Path("D:/v")
    got = _bucket_dir(dest, "2019/../../evil")
    assert dest in got.parents or got.parent == dest
