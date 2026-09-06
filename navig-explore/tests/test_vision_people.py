"""Face grouping — recall, and the merge cascade that recall exposed.

Two failures pull in opposite directions here, and both were real:

* a threshold too strict left **two thirds of all faces in no group at all**;
* loosening it exposed a latent bug where one group absorbed 12,840 photos
  while the next largest held 497.

A false merge is the worse error because it is invisible — the folder still
looks like a person, just fuller — so several of these tests exist purely to
keep that failure loud.
"""
from __future__ import annotations

import numpy as np

from navig_explore.vision import catalog, people


def _lib(tmp_path):
    root = tmp_path / "lib"
    (root / ".mediaexplorer").mkdir(parents=True)
    return root, catalog.connect(root)


def _face(conn, face_id, sha, vec, person_id=None):
    conn.execute(
        """INSERT INTO faces (face_id, sha256, x, y, w, h, score, rotation,
                              engine, dim, vec, person_id)
           VALUES (?,?,0,0,0.2,0.2,0.9,0,'t',?,?,?)""",
        (face_id, sha, len(vec), catalog.pack_vec(vec), person_id))


def _unit(*v):
    a = np.array(v, dtype="float32")
    return a / np.linalg.norm(a)


# ── the calibrated threshold ────────────────────────────────────────────────
def test_threshold_is_a_similarity_not_a_distance():
    """Regression: this was a cosine DISTANCE of 0.38 — i.e. similarity 0.62.

    Measured on 64,446 within-person pairs from the real library, 0.62 captured
    only 30.3% of genuine same-person pairs, which is why two thirds of faces
    never grouped and nothing ever re-attached to a named person.
    """
    assert 0.30 <= people.ASSIGN_MIN_SIMILARITY <= 0.55, (
        "outside the calibrated band: cross-person pairs reach 0.325 at p99, "
        "within-person sit at 0.553 median")


# ── adoption ────────────────────────────────────────────────────────────────
def test_adopt_takes_a_clear_match_and_refuses_a_stranger(tmp_path):
    root, conn = _lib(tmp_path)
    near, far = _unit(1, 0.9, 0, 0), _unit(0, 0, 1, 0)
    conn.execute(
        "INSERT INTO people (person_id, name, locked, dim, centroid) VALUES (1,NULL,0,4,?)",
        (catalog.pack_vec(_unit(1, 1, 0, 0)),))
    _face(conn, 1, "a" * 64, near)     # clearly the same person
    _face(conn, 2, "b" * 64, far)      # clearly not
    conn.commit()

    assert people.adopt_unclustered(conn, quiet=True) == 1
    got = dict(conn.execute("SELECT face_id, person_id FROM faces").fetchall())
    assert got[1] == 1
    assert got[2] is None, "a stranger was adopted"


def test_adopt_refuses_a_face_that_sits_between_two_people(tmp_path):
    """Clearing the bar is not enough — the match must be unambiguous.

    Adopting on "nearest centroid above threshold" alone took coverage from 33%
    to 95% and visibly wrecked the largest groups: one held a woman, two men, a
    monkey mask and a cartoon emoji. A face equidistant from two people is not
    evidence about either, so it stays unassigned — which is at least visible.
    """
    root, conn = _lib(tmp_path)
    a, b = _unit(1, 0, 0, 0), _unit(0, 1, 0, 0)
    conn.execute(
        "INSERT INTO people (person_id, name, locked, dim, centroid) VALUES (1,NULL,0,4,?)",
        (catalog.pack_vec(a),))
    conn.execute(
        "INSERT INTO people (person_id, name, locked, dim, centroid) VALUES (2,NULL,0,4,?)",
        (catalog.pack_vec(b),))
    _face(conn, 1, "a" * 64, _unit(1, 0.98, 0, 0))   # dead between the two
    _face(conn, 2, "b" * 64, _unit(1, 0.05, 0, 0))   # clearly the first person
    conn.commit()

    assert people.adopt_unclustered(conn, quiet=True) == 1
    got = dict(conn.execute("SELECT face_id, person_id FROM faces").fetchall())
    assert got[1] is None, "an ambiguous face was filed under one of two people"
    assert got[2] == 1


def test_margin_is_configured_and_non_zero():
    assert people.ASSIGN_MIN_MARGIN > 0, (
        "without a margin, adoption files ambiguous faces and pollutes groups")


def test_adopt_only_touches_faces_with_no_group(tmp_path):
    """Adoption must never re-home a face that already belongs somewhere."""
    root, conn = _lib(tmp_path)
    conn.execute(
        "INSERT INTO people (person_id, name, locked, dim, centroid) VALUES (1,NULL,0,4,?)",
        (catalog.pack_vec(_unit(1, 1, 0, 0)),))
    conn.execute(
        "INSERT INTO people (person_id, name, locked, dim, centroid) VALUES (2,NULL,0,4,?)",
        (catalog.pack_vec(_unit(1, 1, 0, 0)),))
    _face(conn, 1, "a" * 64, _unit(1, 1, 0, 0), person_id=2)
    conn.commit()

    people.adopt_unclustered(conn, quiet=True)
    assert conn.execute("SELECT person_id FROM faces WHERE face_id=1").fetchone()[0] == 2


# ── the cascade guard ───────────────────────────────────────────────────────
def test_cascade_check_flags_a_lopsided_group(tmp_path):
    """12,840 vs 497 is a merge bug wearing a person's folder name."""
    root, conn = _lib(tmp_path)
    conn.execute("INSERT INTO people (person_id) VALUES (1)")
    conn.execute("INSERT INTO people (person_id) VALUES (2)")
    for i in range(100):
        _face(conn, i + 1, f"{i:064d}", _unit(1, 0, 0, 0), person_id=1)
    for i in range(5):
        _face(conn, 500 + i, f"{i + 900:064d}", _unit(0, 1, 0, 0), person_id=2)
    conn.commit()

    msg = people.check_for_cascade(conn)
    assert msg and "cascade" in msg
    assert "20x" in msg or "20 x" in msg or "20" in msg


def test_cascade_check_stays_quiet_on_a_normal_distribution(tmp_path):
    root, conn = _lib(tmp_path)
    conn.execute("INSERT INTO people (person_id) VALUES (1)")
    conn.execute("INSERT INTO people (person_id) VALUES (2)")
    for i in range(30):
        _face(conn, i + 1, f"{i:064d}", _unit(1, 0, 0, 0), person_id=1)
    for i in range(20):
        _face(conn, 500 + i, f"{i + 900:064d}", _unit(0, 1, 0, 0), person_id=2)
    conn.commit()
    assert people.check_for_cascade(conn) is None


def test_cascade_check_needs_two_groups(tmp_path):
    root, conn = _lib(tmp_path)
    conn.execute("INSERT INTO people (person_id) VALUES (1)")
    _face(conn, 1, "a" * 64, _unit(1, 0, 0, 0), person_id=1)
    conn.commit()
    assert people.check_for_cascade(conn) is None


# ── naming survives ─────────────────────────────────────────────────────────
def test_naming_locks_a_group_against_recluster(tmp_path):
    root, conn = _lib(tmp_path)
    conn.execute("INSERT INTO people (person_id) VALUES (1)")
    _face(conn, 1, "a" * 64, _unit(1, 0, 0, 0), person_id=1)
    _face(conn, 2, "b" * 64, _unit(1, 0.1, 0, 0), person_id=1)
    conn.commit()

    assert people.name(root, 1, "Anna") == 2
    row = conn.execute("SELECT name, locked, n_faces FROM people WHERE person_id=1").fetchone()
    assert (row["name"], row["locked"], row["n_faces"]) == ("Anna", 1, 2)


def test_merge_folds_groups_and_split_releases(tmp_path):
    root, conn = _lib(tmp_path)
    conn.execute("INSERT INTO people (person_id) VALUES (1)")
    conn.execute("INSERT INTO people (person_id) VALUES (2)")
    _face(conn, 1, "a" * 64, _unit(1, 0, 0, 0), person_id=1)
    _face(conn, 2, "b" * 64, _unit(1, 0.1, 0, 0), person_id=2)
    conn.commit()

    assert people.merge(root, 1, 2) == 1
    assert conn.execute("SELECT COUNT(*) FROM faces WHERE person_id=1").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM people WHERE person_id=2").fetchone()[0] == 0

    assert people.split(root, 1) == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM faces WHERE person_id IS NULL").fetchone()[0] == 2


# ── making groups nameable ──────────────────────────────────────────────────
def test_merge_candidates_suggests_but_never_acts(tmp_path):
    """Clustering always over-splits; the fix is a human looking, not a number.

    Measured on the real library the closest pair of large groups sits at 0.795
    and most fall under 0.55 — there is no threshold that separates "one person
    the clusterer split" from "two siblings". Merging on a number is how one
    group swallowed 12,840 faces.
    """
    root, conn = _lib(tmp_path)
    near_a, near_b = _unit(1, 0, 0.02), _unit(1, 0.02, 0)
    far = _unit(0, 1, 0)
    for pid, vec in ((1, near_a), (2, near_b), (3, far)):
        conn.execute("INSERT INTO people (person_id, name, locked) VALUES (?,NULL,0)",
                     (pid,))
        for k in range(12):
            _face(conn, pid * 100 + k, f"{pid}{k:063d}", vec, pid)
    conn.commit()

    pairs = people.merge_candidates(root, min_photos=5)
    assert {p["a"] for p in pairs} | {p["b"] for p in pairs} == {1, 2}, pairs
    assert pairs[0]["similarity"] > 0.9

    # …and nothing was merged.
    assert conn.execute("SELECT COUNT(DISTINCT person_id) FROM faces").fetchone()[0] == 3


def test_contact_sheet_labels_every_row_with_the_id_that_names_it(tmp_path):
    """`people list` prints ids and counts, which says nothing about who anyone is.

    3,491 groups stayed anonymous and `by-person` filled with `person-<id>`
    folders. Naming needs seeing.
    """
    from PIL import Image

    root, conn = _lib(tmp_path)
    (root / "pics").mkdir()
    conn.execute("INSERT INTO people (person_id, name, locked) VALUES (1,NULL,0)")
    for k in range(6):
        sha = f"{k:064d}"
        p = root / "pics" / f"{k}.jpg"
        Image.new("RGB", (200, 200), (k * 30, 80, 120)).save(p)
        conn.execute("""INSERT INTO files (path, root, rel, name, ext, size, mtime,
                                           sha256, present, first_seen)
                        VALUES (?,?,?,?,'.jpg',10,0,?,1,0)""",
                     (str(p), str(root), f"pics/{k}.jpg", f"{k}.jpg", sha))
        _face(conn, k, sha, _unit(1, 0, 0), 1)
    conn.commit()

    out = tmp_path / "sheet.jpg"
    stats = people.contact_sheet(root, out, limit=10, per_group=4, thumb=64,
                                 min_photos=3)
    assert stats["groups"] == 1 and stats["faces"] == 4
    assert out.exists() and Image.open(out).size[0] > 64


def test_a_group_below_min_photos_is_not_worth_a_row(tmp_path):
    root, conn = _lib(tmp_path)
    conn.execute("INSERT INTO people (person_id, name, locked) VALUES (1,NULL,0)")
    _face(conn, 1, "a" * 64, _unit(1, 0, 0), 1)
    conn.commit()
    assert people.contact_sheet(root, tmp_path / "s.jpg", min_photos=5)["groups"] == 0
