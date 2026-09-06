"""Structured search filters.

These run without torch: every case here is filter-only or works off vectors
written directly into the catalog, so the suite stays fast and does not need a
GPU or a model download. The text-query path is exercised by hand against the
real library; what is worth pinning here is the filter algebra, where a wrong
intersection silently returns a plausible-looking subset.
"""
from __future__ import annotations

import numpy as np
import pytest

from navig_explore.vision import catalog, search as S


@pytest.fixture()
def lib(tmp_path):
    """A tiny catalog: three photos, two people, two years, two classes."""
    root = tmp_path / "lib"
    (root / ".mediaexplorer").mkdir(parents=True)
    conn = catalog.connect(root)

    conn.execute("INSERT INTO people (person_id, name, locked) VALUES (1,'Anna',1)")
    conn.execute("INSERT INTO people (person_id, name, locked) VALUES (2,'Bruno',1)")

    rows = [
        # sha,       rel,        class,   year,   person, confidence
        ("a" * 64, "one.jpg",   "photo",  "2009", 1, 0.95),
        ("b" * 64, "two.jpg",   "photo",  "2014", 2, 0.95),
        ("c" * 64, "three.jpg", "webcam", "2009", 1, 0.60),
    ]
    for i, (sha, rel, klass, year, person, conf) in enumerate(rows):
        conn.execute(
            """INSERT INTO files (path, root, rel, name, ext, size, mtime, sha256,
                                  present, first_seen)
               VALUES (?,?,?,?,?,?,?,?,1,'now')""",
            (str(root / rel).lower(), str(root.resolve()), rel, rel, ".jpg",
             100, 0.0, sha))
        conn.execute("INSERT INTO assets (sha256, decoded, w, h) VALUES (?,1,800,600)",
                     (sha,))
        conn.execute("INSERT INTO classes (sha256, class, score) VALUES (?,?,0.5)",
                     (sha, klass))
        conn.execute(
            """INSERT INTO dates (sha256, value, source, confidence)
               VALUES (?,?,?,?)""",
            (sha, f"{year}-06-01T12:00:00", "exif" if conf > 0.9 else "folder", conf))
        conn.execute(
            """INSERT INTO faces (sha256,x,y,w,h,score,rotation,engine,dim,vec,person_id)
               VALUES (?,0.1,0.1,0.2,0.2,0.9,0,'t',4,?,?)""",
            (sha, catalog.pack_vec(np.eye(4, dtype="float32")[i % 4]), person))
        conn.execute(
            "INSERT INTO geo (sha256, lat, lon, place, country, source, confidence) "
            "VALUES (?,?,?,?,?,?,?)",
            (sha, 43.6, 3.8, "Montpellier", "FR", "exif", 1.0))
    conn.commit()
    return root


def _names(hits):
    return sorted(h["name"] for h in hits)


def test_filter_by_person(lib):
    assert _names(S.search(lib, None, person="Anna")) == ["one.jpg", "three.jpg"]


def test_filter_by_year(lib):
    assert _names(S.search(lib, None, year=2014)) == ["two.jpg"]


def test_filter_by_class(lib):
    assert _names(S.search(lib, None, klass="webcam")) == ["three.jpg"]


def test_filter_by_place(lib):
    assert len(S.search(lib, None, place="montpellier")) == 3


def test_filters_intersect_rather_than_union(lib):
    """The failure that matters: an OR here returns a plausible wrong answer."""
    assert _names(S.search(lib, None, person="Anna", year=2009)) == \
        ["one.jpg", "three.jpg"]
    assert _names(S.search(lib, None, person="Anna", klass="photo")) == ["one.jpg"]
    assert S.search(lib, None, person="Anna", year=2014) == []


def test_year_filter_respects_confidence_floor(lib):
    """three.jpg is dated 2009 only by a weak folder guess — excluded at 0.9."""
    strong = _names(S.search(lib, None, year=2009, min_confidence=0.9))
    assert strong == ["one.jpg"]


def test_unknown_person_returns_empty_not_everything(lib):
    assert S.search(lib, None, person="Nobody") == []


def test_results_carry_their_date_provenance(lib):
    hit = S.search(lib, None, klass="webcam")[0]
    assert hit["date_source"] == "folder"
    assert hit["date_confidence"] == pytest.approx(0.60)
    assert hit["people"] == ["Anna"]


def test_similarity_search_needs_a_known_file(lib):
    """A typo'd path must say so, not return an empty list that reads as an answer."""
    with pytest.raises(ValueError, match="not in the catalog"):
        S.search(lib, None, like=str(lib / "does-not-exist.jpg"))


def test_text_query_without_embeddings_is_an_error_not_silence(lib):
    """"No matches" and "you never indexed this" must not look identical."""
    with pytest.raises(ValueError, match="no embeddings"):
        S.search(lib, "a red car")
