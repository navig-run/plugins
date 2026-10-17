"""Subject tagging — multi-label, and why one global threshold cannot work.

Content classes are argmax because a file is exactly one of photo/screenshot/
webcam. Subjects are not: dinner on a terrace at sunset is food AND sunset AND
architecture. These tests pin that difference, and the per-subject bar that makes
it usable.
"""
from __future__ import annotations

import numpy as np
import pytest

from navig_explore.vision import catalog, subjects as S


def _lib(tmp_path):
    root = tmp_path / "lib"
    (root / ".mediaexplorer").mkdir(parents=True)
    conn = catalog.connect(root)
    S._ensure_table(conn)
    return root, conn


# ── the taxonomy itself ─────────────────────────────────────────────────────
def test_taxonomy_is_flat_unique_and_non_empty():
    pairs = S.subjects()
    names = [s for _g, s in pairs]
    assert len(names) == len(set(names)), "a subject name is duplicated across groups"
    assert len(names) >= 40, "the taxonomy is too small to be useful"
    for group, subs in S.TAXONOMY.items():
        assert group and subs
        for name, prompts in subs.items():
            assert prompts, f"{name} has no prompts"
            assert all(isinstance(p, str) and p.strip() for p in prompts)


def test_the_subjects_the_operator_asked_for_exist():
    names = {s for _g, s in S.subjects()}
    for wanted in ("sunset", "food", "sea", "beach", "car", "cat", "dog",
                   "portrait", "concert", "snow", "flowers", "city"):
        assert wanted in names


def test_bucket_labels_nest_by_group():
    """`nature/sunset` becomes two folders, not one with a dash in it."""
    from navig_explore.vision.arrange import _bucket_dir
    from pathlib import Path

    got = _bucket_dir(Path("D:/v"), "nature/sunset")
    assert got == Path("D:/v/nature/sunset")


def test_a_label_cannot_escape_the_destination():
    from navig_explore.vision.arrange import _bucket_dir
    from pathlib import Path

    dest = Path("D:/v")
    got = _bucket_dir(dest, "../../evil")
    assert dest in got.parents or got.parent == dest


# ── the per-subject bar ─────────────────────────────────────────────────────
def test_one_global_threshold_would_be_wrong():
    """Each prompt set has its own centre — that is the whole reason for sigma.

    Two subjects with very different baselines must both be able to fire.
    """
    sims = np.array([
        [0.30, 0.05],   # photo 0: high on A's scale, low on B's
        [0.29, 0.04],
        [0.31, 0.06],
        [0.45, 0.05],   # photo 3: a clear A
        [0.30, 0.20],   # photo 4: a clear B, but B's absolute score is far lower
    ], dtype="float32")
    mean, std = sims.mean(axis=0), sims.std(axis=0)
    bar = mean + 1.5 * std
    assert sims[3, 0] >= bar[0], "the standout A did not clear A's own bar"
    assert sims[4, 1] >= bar[1], "the standout B did not clear B's own bar"
    # A single global cut at A's level would silence B entirely.
    assert sims[4, 1] < bar[0]


def test_max_per_photo_is_bounded():
    assert 1 <= S.MAX_PER_PHOTO <= 6, "too many labels stop meaning anything"


def test_a_percentile_cap_used_as_the_primary_bar_pins_every_subject_to_one_count():
    """Why the cap had to stop being the rule.

    It was introduced because sigma alone admits roughly the same COUNT into
    every subject — and then did exactly the same thing itself, harder: measured
    on the real library the 0.005 quantile sat ABOVE the sigma bar for 41 of 43
    subjects, so all 43 were admitted at exactly 294 files. A cap asserts that a
    library holds as many books as sunsets, which no library does.
    """
    rng = np.random.default_rng(0)
    # Two subjects with very different real prevalence.
    common = rng.normal(0.09, 0.012, size=(5000, 1)).astype("float32")
    common[:400] += 0.05                     # 400 genuine examples
    rare = rng.normal(0.09, 0.012, size=(5000, 1)).astype("float32")
    rare[:8] += 0.05                         # 8 genuine examples

    counts = []
    for sims in (common, rare):
        pct_bar = np.quantile(sims, 1.0 - 0.005, axis=0)
        counts.append(int((sims >= pct_bar).sum()))
    assert counts[0] == counts[1], (
        "this is the defect: the cap gives the rare subject as many photos as "
        "the common one, so the rare folder is mostly tail")


def test_the_row_bar_rejects_a_photo_that_is_confidently_nothing():
    """The failure a per-subject bar structurally cannot see.

    A dark or blurred frame sits near the centre of embedding space and scores
    middling on all 43 prompts. Down the column it can look unusual; across its
    own row it is flat. That flatness is what put a tree mural, a pergola and
    several near-black rooms in `books`.
    """
    rng = np.random.default_rng(1)
    sims = rng.normal(0.09, 0.012, size=(2000, 43)).astype("float32")
    sims[0] = 0.128                          # flat: high everywhere, about nothing
    sims[1] = 0.09
    sims[1, 7] = 0.145                       # peaked: confidently subject 7

    col = (sims - sims.mean(axis=0)) / sims.std(axis=0)
    row = ((sims - sims.mean(axis=1, keepdims=True))
           / sims.std(axis=1, keepdims=True))

    assert (col[0] >= S.DEFAULT_SIGMA).any(), "the flat frame does clear a column bar"
    keep = (col >= S.DEFAULT_SIGMA) & (row >= S.DEFAULT_ROW_SIGMA)
    assert not keep[0].any(), "a flat score profile must earn no label at all"
    assert keep[1, 7], "a peaked score profile must still earn its label"


def test_top_fraction_is_off_by_default():
    assert S.DEFAULT_TOP_FRACTION == 0, (
        "as a primary bar this pinned every subject to the same count; it is a "
        "ceiling to reach for, not the rule")


def test_both_sigma_defaults_are_selective():
    assert S.DEFAULT_SIGMA >= 2.0, (
        "a low sigma tags almost everything, which is how the content classes "
        "filled folders with confident nonsense")
    assert S.DEFAULT_ROW_SIGMA >= 2.5, (
        "the row bar is what removes the low-information tail; loosening it "
        "brings the tree mural back")


# ── storage ─────────────────────────────────────────────────────────────────
def test_table_is_multi_label(tmp_path):
    """One photo, several subjects — the schema must allow it."""
    _root, conn = _lib(tmp_path)
    sha = "a" * 64
    for grp, name in (("nature", "sunset"), ("things", "food"), ("nature", "sea")):
        conn.execute(
            "INSERT INTO subjects (sha256, grp, subject, score) VALUES (?,?,?,0.4)",
            (sha, grp, name))
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM subjects WHERE sha256=?", (sha,)).fetchone()[0]
    assert n == 3


def test_same_subject_twice_is_idempotent(tmp_path):
    _root, conn = _lib(tmp_path)
    for _ in range(2):
        conn.execute("""INSERT OR REPLACE INTO subjects (sha256, grp, subject, score)
                        VALUES (?,?,?,?)""", ("b" * 64, "nature", "sunset", 0.5))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM subjects").fetchone()[0] == 1


def test_tag_needs_embeddings(tmp_path):
    root, conn = _lib(tmp_path)
    conn.execute("INSERT INTO classes (sha256, class, score) VALUES (?,'photo',1.0)",
                 ("c" * 64,))
    conn.commit()
    with pytest.raises(RuntimeError, match="no embeddings"):
        S.tag(root, quiet=True)
