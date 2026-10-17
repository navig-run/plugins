"""Folder-name geocoding — the rung where a plausible answer is the danger.

Matching folder names against a world gazetteer is high-yield and high-risk: the
world contains a populated place called almost every word. Run naively over a
real 97,600-file library it tagged **68,351 photos as "Date, Japan"**, because the
library's own top-level folder is called `By Date`. These tests pin the three
rules that took folder matching from ~1% precision to ~100%.
"""
from __future__ import annotations

import pytest

from navig_explore.vision import catalog, places


# (name, admin1, country, lat, lon, population)
GAZ = [
    ("Date", "01", "JP", 42.4, 140.8, 36_000),
    ("Mexico", "B0", "PH", 15.0, 120.7, 154_000),
    ("London", "ENG", "GB", 51.5, -0.12, 8_961_989),
    ("London", "08", "CA", 42.9, -81.2, 346_000),
    ("Martin", "03", "SK", 49.0, 18.9, 55_000),
    ("Frontignan", "76", "FR", 43.4, 3.75, 24_000),
    ("Montpellier", "76", "FR", 43.6, 3.87, 285_000),
    ("Minsk", "04", "BY", 53.9, 27.5, 1_742_000),
    ("Amsterdam", "07", "NL", 52.37, 4.89, 741_000),
    ("Toma", "05", "BF", 12.7, -2.9, 16_000),
]


def _lib(tmp_path, rels, exif_countries=("FR",)):
    root = tmp_path / "lib"
    (root / ".mediaexplorer").mkdir(parents=True)
    conn = catalog.connect(root)
    for i, rel in enumerate(rels):
        sha = f"{i:064d}"
        conn.execute(
            """INSERT INTO files (path, root, rel, name, ext, size, mtime, sha256,
                                  present, first_seen)
               VALUES (?,?,?,?,?,?,?,?,1,'now')""",
            (str(root / rel).lower(), str(root.resolve()), rel, rel.split("/")[-1],
             ".jpg", 10, 0.0, sha))
    for j, cc in enumerate(exif_countries):
        conn.execute(
            """INSERT INTO geo (sha256, lat, lon, place, country, source, confidence)
               VALUES (?,?,?,?,?,'exif',1.0)""",
            (f"exif{j}", 43.6, 3.87, "Montpellier", cc))
    conn.commit()
    return root, conn


def test_structural_folders_are_detected_from_the_data(tmp_path):
    """`By Date` shelters most of the library, so it is shelving, not a place.

    Individual event folders each hold a small slice, which is exactly what keeps
    them matchable — the rule separates the shelf from what is on it.
    """
    rels = [f"By Date/2009/event{i // 3}/{i}.jpg" for i in range(60)]
    rels += ["Albums/x/1.jpg"]
    root, conn = _lib(tmp_path, rels)
    struct = places.structural_components(conn, root, share=0.02, minimum=5)
    assert "by date" in struct
    assert "event0" not in struct, "a genuine event folder must stay matchable"
    assert "event19" not in struct


def test_structural_detection_finds_only_shelving_on_the_real_shape(tmp_path):
    """Regression guard for the rule's selectivity.

    On the real 97,600-file library this flags 21 components — every one of them
    a year, `by date`, `albums`, `_undated`, or a person-named folder. Zero places
    were suppressed. Here: a place folder must survive even when it is popular.
    """
    rels = [f"By Date/2009/minsk/{i}.jpg" for i in range(3)]
    rels += [f"By Date/{2000 + i}/ev{i}/{i}.jpg" for i in range(60)]
    root, conn = _lib(tmp_path, rels)
    struct = places.structural_components(conn, root, share=0.02, minimum=5)
    assert "by date" in struct
    assert "minsk" not in struct


def test_never_a_place_covers_library_vocabulary():
    for word in ("date", "albums", "general", "people", "camera", "orange"):
        assert word in places._NEVER_A_PLACE


def test_home_countries_come_from_measured_gps(tmp_path):
    _root, conn = _lib(tmp_path, ["a.jpg"], exif_countries=("FR", "BY"))
    assert places.home_countries(conn) == {"FR", "BY"}


# ── candidate choice ────────────────────────────────────────────────────────
def test_home_country_beats_population(tmp_path):
    """London is bigger in GB, but Frontignan must survive on the country prior."""
    cands = places._candidates(GAZ, min_population=places.FOLDER_MIN_POPULATION)
    idx = places._pick(GAZ, cands["frontignan"], {"FR"})
    assert idx is not None and GAZ[idx][:3] == ("Frontignan", "76", "FR")


def test_most_populous_wins_among_equals(tmp_path):
    """London, Canada was being chosen over London, England."""
    cands = places._candidates(GAZ, min_population=places.FOLDER_MIN_POPULATION)
    idx = places._pick(GAZ, cands["london"], set())
    assert GAZ[idx][2] == "GB"


@pytest.mark.parametrize("word", ["martin", "toma", "mexico"])
def test_a_stranger_that_is_not_famous_is_rejected(word):
    """No candidate in a home country and not world-famous → not a place at all.

    Reordering candidates is not enough: without an outright rejection these
    three tagged 475 photos as Slovakia, Burkina Faso and the Philippines.
    """
    cands = places._candidates(GAZ, min_population=places.FOLDER_MIN_POPULATION)
    assert places._pick(GAZ, cands[word], {"FR", "BY", "NL"}) is None


def test_a_famous_stranger_is_still_accepted():
    """Amsterdam is unambiguous enough to accept without a country prior."""
    cands = places._candidates(GAZ, min_population=places.FOLDER_MIN_POPULATION)
    idx = places._pick(GAZ, cands["amsterdam"], {"FR"})
    assert idx is not None and GAZ[idx][0] == "Amsterdam"


def test_population_floor_keeps_the_home_regions_small_towns():
    """A 50,000 floor discarded Frontignan and Millau — real places in this library."""
    assert places.FOLDER_MIN_POPULATION <= 24_000
    cands = places._candidates(GAZ, min_population=places.FOLDER_MIN_POPULATION)
    assert "frontignan" in cands


def test_confidence_keeps_folder_guesses_below_the_move_threshold():
    """A place read off a folder name must never be grounds to move a file."""
    from navig_explore.vision import triage

    assert places.CONFIDENCE["folder"] < triage.MOVE_MIN_CONFIDENCE
    assert places.CONFIDENCE["exif"] > places.CONFIDENCE["propagated"] > \
        places.CONFIDENCE["folder"]
