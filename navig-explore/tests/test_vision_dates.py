"""The date ladder — every rung, and the two guards that stop it lying.

These tests exist because each one corresponds to a way this module was observed
to be wrong against the real 4,888-file recovery folder it was built for, not to
a hypothetical.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from navig_explore.vision import dates as D


# ── month tokens ────────────────────────────────────────────────────────────
@pytest.mark.parametrize(("token", "month"), [
    ("мар", 3), ("mar", 3), ("map", 3),      # Cyrillic, Latin, OCR-confused
    ("апр", 4), ("anp", 4), ("app", 4),      # Cyrillic п reads as Latin n
    ("май", 5),                              # NFKD used to decompose й and lose this
    ("авг", 8), ("abg", 8),
    ("дек", 12), ("dek", 12),
    ("янв", 1), ("фев", 2), ("окт", 10), ("ноя", 11),
])
def test_month_tokens_across_scripts(token, month):
    assert D.month_from_token(token) == month


def test_month_token_rejects_noise():
    assert D.month_from_token("") is None
    assert D.month_from_token("sensor") is None


def test_may_is_not_march():
    """Regression: NFKD split "май" into и+breve, and fuzzy matching chose "map"."""
    assert D.month_from_token("май") == 5
    assert D.month_from_token("май") != D.month_from_token("мар")


def test_april_is_not_march():
    """Regression: "anp" fuzzy-matched "map" before п→n was a known confusable."""
    assert D.month_from_token("anp") == 4


# ── overlay parsing ─────────────────────────────────────────────────────────
@pytest.mark.parametrize(("text", "expect"), [
    ("мар 28, 2005 03:37 sensor:18", datetime(2005, 3, 28, 3, 37)),
    ("map 28, 2005 02:52 sensor:34 0088", datetime(2005, 3, 28, 2, 52)),
    ("дек 09, 2005 21:08 sensor 40", datetime(2005, 12, 9, 21, 8)),
    ("2005-03-28 03:37:11", datetime(2005, 3, 28, 3, 37, 11)),
    ("28.03.2005 03:37", datetime(2005, 3, 28, 3, 37)),
])
def test_parse_overlay(text, expect):
    assert D.parse_overlay(text) == expect


def test_parse_overlay_rejects_garbage():
    assert D.parse_overlay("no timestamp here") is None
    assert D.parse_overlay("") is None


# ── filenames ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize(("name", "y", "m", "d"), [
    ("IMG_20180712_143012.jpg", 2018, 7, 12),
    ("IMG-20180712-WA0001.jpg", 2018, 7, 12),
    ("Screenshot 2019-03-04 at 11.22.33.png", 2019, 3, 4),
    ("photo_22@26-04-2026_14-07-45.jpg", 2026, 4, 26),
])
def test_from_filename(name, y, m, d):
    got = D.from_filename(name)
    assert (got.year, got.month, got.day) == (y, m, d)


@pytest.mark.parametrize("name", ["file_49446.jpg", "LostFile_JPG_11486575.JPG",
                                  "1002645_1434819296755256_390707905_n.jpg"])
def test_from_filename_refuses_recovery_names(name):
    """Carver names and Facebook CDN ids are digits, not dates."""
    assert D.from_filename(name) is None


# ── folders ─────────────────────────────────────────────────────────────────
def test_from_folder_reads_named_event_year():
    got, part, precision = D.from_folder("By Date/2009/Alzon 2009/f.jpg")
    assert got.year == 2009 and part == "Alzon 2009"
    assert precision == D.YEAR, "a folder year says nothing about the day"


def test_from_folder_reads_cyrillic_event_folder():
    got, _part, _p = D.from_folder("By Date/2014/Берлин 2014/z.jpg")
    assert got.year == 2014


def test_from_folder_reads_day_folder():
    got, _part, precision = D.from_folder("By Date/2003/22.11.03 - Chez Damien/x.jpg")
    assert (got.year, got.month, got.day) == (2003, 11, 22)
    assert precision == D.DAY


def test_a_year_only_claim_is_not_mistakable_for_a_measured_day():
    """`Alzon 2007` is stored as noon on 1 July 2007 — a date nothing happened on.

    Reading it back as a day put fourteen different 2007 occasions under one
    fabricated `2007-07-01` prefix: Airsoft, Alzon, Noel, Jour de lan, Cabananéné.
    The value has to carry how much of it was measured.
    """
    dt, _part, precision = D.from_folder("By Date/2007/Airsoft/f.jpg")
    assert (dt.month, dt.day, dt.hour) == (7, 1, 12), "the placeholder shape"
    assert precision == D.YEAR
    assert D.precision_of("folder", "By Date/2007/Airsoft/f.jpg") == D.YEAR
    assert D.precision_of("folder", "By Date/2003/22.11.03 - x/f.jpg") == D.DAY
    # Every other rung reads a real timestamp or a written-out date.
    for src in ("exif", "overlay", "filename", "twin", "sibling", "mtime"):
        assert D.precision_of(src, "By Date/2007/Airsoft/f.jpg") == D.DAY


def test_from_folder_returns_nothing_without_a_date():
    assert D.from_folder("Albums/Themes/country/minsk/y.jpg")[0] is None


# ── EXIF sanity ─────────────────────────────────────────────────────────────
def test_parse_exif_accepts_real_dates():
    assert D.parse_exif("2005:03:28 03:37:11") == datetime(2005, 3, 28, 3, 37, 11)


@pytest.mark.parametrize("value", [
    "0000:00:00 00:00:00",   # null EXIF block
    "2031:07:09 13:36:06",   # camera clock never set — the future
    "1970:02:22 08:18:41",   # camera clock at epoch zero
    "",
])
def test_parse_exif_rejects_impossible_dates(value):
    """28 of 33 EXIF dates in the real folder are one of these. None is a date."""
    assert D.parse_exif(value) is None


# ── the junk-mtime guards ───────────────────────────────────────────────────
def _db(tmp_path):
    from navig_explore.vision import catalog
    root = tmp_path / "lib"
    (root / ".mediaexplorer").mkdir(parents=True)
    return root, catalog.connect(root)


def _add_file(conn, root, path, mtime, sha):
    conn.execute(
        """INSERT INTO files (path, root, rel, name, ext, size, mtime, sha256,
                              present, first_seen)
           VALUES (?,?,?,?,?,?,?,?,1,'now')""",
        (path, str(root.resolve()), path, path, ".jpg", 100, mtime, sha))


def test_junk_minutes_finds_a_write_burst(tmp_path):
    """569 files in one minute is a disk writer, not a shutter."""
    root, conn = _db(tmp_path)
    base = (1287700000 // 60) * 60             # align, so the burst is ONE bucket
    for i in range(200):                       # 200 files inside that minute
        _add_file(conn, root, f"burst{i}.jpg", base + (i % 60), f"s{i}")
    for i in range(20):                        # genuine captures, minutes apart
        _add_file(conn, root, f"real{i}.jpg", base + 100000 + i * 300, f"r{i}")
    conn.commit()
    junk = D.junk_minutes(conn, root, per_minute=90)
    assert base // 60 in junk
    assert (base + 100000) // 60 not in junk


def test_mtime_rejected_when_it_contradicts_measured_dates(tmp_path):
    """The self-validating rule: ask the files whose date we already KNOW."""
    root, conn = _db(tmp_path)
    recovery_run = datetime(2010, 10, 22, 7, 45)
    for i in range(30):
        sha = f"sha{i}"
        _add_file(conn, root, f"f{i}.jpg", recovery_run.timestamp(), sha)
        conn.execute(
            "INSERT INTO dates (sha256, value, source, confidence) VALUES (?,?,?,?)",
            (sha, datetime(2005, 2, 15, 12, 0).isoformat(), "overlay", 0.95))
    conn.commit()
    ok, why = D.mtime_is_trustworthy(conn, root)
    assert ok is False
    assert "agreement" in why


def test_mtime_kept_when_it_agrees(tmp_path):
    root, conn = _db(tmp_path)
    when = datetime(2018, 7, 12, 14, 30)
    for i in range(30):
        sha = f"sha{i}"
        _add_file(conn, root, f"f{i}.jpg", (when + timedelta(seconds=i)).timestamp(), sha)
        conn.execute(
            "INSERT INTO dates (sha256, value, source, confidence) VALUES (?,?,?,?)",
            (sha, when.isoformat(), "exif", 1.0))
    conn.commit()
    ok, _ = D.mtime_is_trustworthy(conn, root)
    assert ok is True


def test_mtime_allowed_when_there_is_nothing_to_judge_by(tmp_path):
    """With no measured dates the honest answer is "don't know", not "reject"."""
    root, conn = _db(tmp_path)
    _add_file(conn, root, "a.jpg", 1531405800, "sha-a")
    conn.commit()
    ok, why = D.mtime_is_trustworthy(conn, root)
    assert ok is True
    assert "not enough" in why


# ── confidence ordering is the contract everything downstream relies on ─────
def test_confidence_ranks_measured_above_inferred():
    c = D.CONFIDENCE
    assert c["exif"] > c["overlay"] > c["filename"] > c["twin"]
    assert c["twin"] > c["folder"] > c["sibling"] > c["mtime"]


def test_move_threshold_excludes_the_weak_rungs():
    """Only measured-ish evidence may ever move a file."""
    from navig_explore.vision import triage as T
    for weak in ("folder", "sibling", "mtime"):
        assert D.CONFIDENCE[weak] < T.MOVE_MIN_CONFIDENCE
    for strong in ("exif", "overlay", "filename", "twin"):
        assert D.CONFIDENCE[strong] >= T.MOVE_MIN_CONFIDENCE
