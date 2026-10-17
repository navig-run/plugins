"""Folder views built from links — the invariants that keep them safe.

`arrange` exists so "show me folders per person" never means "dismantle the
curated event folders". Every test here guards a way that promise could break.
"""
from __future__ import annotations

import json
import os

import pytest

from navig_explore.vision import arrange, catalog


def _write_meta(root, rows):
    side = root / ".mediaexplorer"
    side.mkdir(parents=True, exist_ok=True)
    with (side / "meta.jsonl").open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _lib(tmp_path, files):
    """files: [(rel, sha, person|None, date|None, conf)]"""
    root = tmp_path / "lib"
    root.mkdir()
    _write_meta(root, [
        {"rel": rel, "abs": str(root / rel), "name": rel.split("/")[-1], "ext": ".jpg",
         "type": "image", "size": 10, "mtime": 0.0, "probe_ok": True, "w": 800,
         "h": 600, "created": "", "camera": "", "gps": None, "res_tier": "SD",
         "source_class": "x"}
        for rel, *_ in files])
    conn = catalog.connect(root)
    catalog.ingest_meta_jsonl(conn, root, quiet=True)
    conn.execute("INSERT INTO people (person_id, name, locked) VALUES (1,'Anna',1)")
    conn.execute("INSERT INTO people (person_id, name, locked) VALUES (2,'Bruno',1)")
    for rel, sha, person, date, conf in files:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"pixels-" + sha.encode())
        conn.execute("UPDATE files SET sha256=? WHERE rel=?", (sha, rel))
        conn.execute("INSERT OR IGNORE INTO assets (sha256, decoded) VALUES (?,1)", (sha,))
        if person:
            conn.execute("""INSERT INTO faces (sha256,x,y,w,h,score,rotation,engine,
                                               person_id) VALUES (?,0,0,1,1,.9,0,'t',?)""",
                         (sha, person))
        if date:
            conn.execute("""INSERT OR REPLACE INTO dates (sha256, value, source, confidence)
                            VALUES (?,?,'exif',?)""", (sha, date, conf))
    conn.commit()
    return root, conn


def test_refuses_a_destination_inside_the_library(tmp_path):
    """A link farm inside the library corrupts the next index and dedup run."""
    root, _ = _lib(tmp_path, [("a.jpg", "a" * 64, 1, None, 0)])
    with pytest.raises(arrange.DestinationInsideLibrary):
        arrange.build_plan(root, root / "_by-person", "person")


def test_force_allows_it_for_someone_who_means_it(tmp_path):
    root, _ = _lib(tmp_path, [("a.jpg", "a" * 64, 1, None, 0)])
    rows, _s = arrange.build_plan(root, root / "_by-person", "person", force=True)
    assert rows


def test_link_shares_the_original_and_never_moves_it(tmp_path):
    root, _ = _lib(tmp_path, [("event/a.jpg", "a" * 64, 1, None, 0)])
    dest = tmp_path / "views"
    rows, _s = arrange.build_plan(root, dest, "person")
    arrange.apply_plan(rows, tmp_path / "log.csv")

    original = root / "event" / "a.jpg"
    link = dest / "Anna" / "a.jpg"
    assert original.exists(), "the original must stay exactly where it was"
    assert link.exists()
    assert os.path.samefile(original, link), "should be a hard link, not a copy"


def test_a_photo_of_two_people_appears_in_both_folders(tmp_path):
    """The reason this uses links rather than moves at all."""
    root, conn = _lib(tmp_path, [("a.jpg", "a" * 64, 1, None, 0)])
    conn.execute("""INSERT INTO faces (sha256,x,y,w,h,score,rotation,engine,person_id)
                    VALUES (?,0,0,1,1,.9,0,'t',2)""", ("a" * 64,))
    conn.commit()
    dest = tmp_path / "views"
    rows, summary = arrange.build_plan(root, dest, "person")
    arrange.apply_plan(rows, tmp_path / "log.csv")
    assert (dest / "Anna" / "a.jpg").exists()
    assert (dest / "Bruno" / "a.jpg").exists()
    assert summary == {"Anna": 1, "Bruno": 1}


def test_same_filename_different_photos_both_survive(tmp_path):
    """Regression: 27 photos vanished from a real view.

    Two different photos can share a name (IMG_0001.JPG from two event folders).
    Skipping on 'destination exists' silently dropped the second one.
    """
    root, _ = _lib(tmp_path, [("jan/IMG_1.jpg", "a" * 64, 1, None, 0),
                              ("feb/IMG_1.jpg", "b" * 64, 1, None, 0)])
    dest = tmp_path / "views"
    rows, _s = arrange.build_plan(root, dest, "person")
    stats = arrange.apply_plan(rows, tmp_path / "log.csv")

    assert stats["linked"] == 2, "a distinct photo was dropped on a name collision"
    got = sorted(p.name for p in (dest / "Anna").iterdir())
    assert got == ["IMG_1.jpg", "IMG_1__1.jpg"]


def test_rerunning_is_idempotent(tmp_path):
    root, _ = _lib(tmp_path, [("a.jpg", "a" * 64, 1, None, 0)])
    dest = tmp_path / "views"
    rows, _s = arrange.build_plan(root, dest, "person")
    arrange.apply_plan(rows, tmp_path / "log1.csv")
    stats = arrange.apply_plan(rows, tmp_path / "log2.csv")
    assert stats["linked"] == 0 and stats["existed"] == 1
    assert len(list((dest / "Anna").iterdir())) == 1, "a second run duplicated entries"


def test_year_view_ignores_weakly_dated_photos(tmp_path):
    root, _ = _lib(tmp_path, [
        ("sure.jpg", "a" * 64, None, "2009-06-01T12:00:00", 1.0),
        ("guess.jpg", "b" * 64, None, "2014-07-01T12:00:00", 0.60),
    ])
    rows, summary = arrange.build_plan(root, tmp_path / "views", "year")
    assert summary == {"2009": 1}, "a folder-year guess must not file a photo"


def test_remove_deletes_the_view_and_keeps_the_originals(tmp_path):
    root, _ = _lib(tmp_path, [("event/a.jpg", "a" * 64, 1, None, 0)])
    dest = tmp_path / "views"
    log = tmp_path / "log.csv"
    rows, _s = arrange.build_plan(root, dest, "person")
    arrange.apply_plan(rows, log)

    stats = arrange.remove(log)
    assert stats["unlinked"] == 1
    assert not (dest / "Anna" / "a.jpg").exists()
    assert (root / "event" / "a.jpg").read_bytes().startswith(b"pixels"), \
        "removing a view must never touch the original"


# ── syncing instead of rebuilding ───────────────────────────────────────────
def test_rerunning_writes_nothing_and_removes_nothing(tmp_path):
    """The whole point: an unchanged plan must be a no-op, not 147k relinks."""
    root, _ = _lib(tmp_path, [("event/a.jpg", "a" * 64, 1, None, 0)])
    dest = tmp_path / "views"
    rows, _s = arrange.build_plan(root, dest, "person")

    first = arrange.apply_plan(rows, tmp_path / "l1.csv", dest=dest, prune=True)
    assert first["linked"] == 1 and first.get("pruned", 0) == 0

    second = arrange.apply_plan(rows, tmp_path / "l2.csv", dest=dest, prune=True)
    assert second["linked"] == 0, "an unchanged entry must not be recreated"
    assert second["existed"] == 1
    assert second.get("pruned", 0) == 0, "…nor removed and remade"


def test_a_bucket_that_changed_name_moves_and_leaves_nothing_behind(tmp_path):
    """Renaming an event used to mean deleting the whole tree to rebuild it."""
    root, conn = _lib(tmp_path, [("event/a.jpg", "a" * 64, 1, None, 0)])
    dest = tmp_path / "views"
    rows, _s = arrange.build_plan(root, dest, "person")
    arrange.apply_plan(rows, tmp_path / "l1.csv", dest=dest, prune=True)
    assert (dest / "Anna" / "a.jpg").exists()

    conn.execute("UPDATE people SET name='Anna Smith' WHERE person_id=1")
    conn.commit()
    rows2, _s2 = arrange.build_plan(root, dest, "person")
    stats = arrange.apply_plan(rows2, tmp_path / "l2.csv", dest=dest, prune=True)

    assert (dest / "Anna Smith" / "a.jpg").exists()
    assert not (dest / "Anna").exists(), "the old folder must not linger"
    assert stats["linked"] == 1 and stats["pruned"] == 1
    assert (root / "event" / "a.jpg").exists(), "the original is never touched"


def test_pruning_never_removes_the_trees_own_notes(tmp_path):
    root, _ = _lib(tmp_path, [("event/a.jpg", "a" * 64, 1, None, 0)])
    dest = tmp_path / "views"
    dest.mkdir()
    (dest / "_READ-ME-FIRST.md").write_text("hi", encoding="utf-8")
    (dest / "_lost-occasions.txt").write_text("hi", encoding="utf-8")
    rows, _s = arrange.build_plan(root, dest, "person")
    arrange.apply_plan(rows, tmp_path / "l.csv", dest=dest, prune=True)
    assert (dest / "_READ-ME-FIRST.md").exists()
    assert (dest / "_lost-occasions.txt").exists()


def test_a_colliding_filename_does_not_gain_a_copy_per_run(tmp_path):
    """The bug that made syncing impossible, hidden by deleting the tree first.

    Two different photos called IMG_1.jpg in one bucket: the second is stored as
    `IMG_1__1.jpg`. `_unique` only looked for a FREE name, so the next run found
    that taken and made `IMG_1__2.jpg`, and the run after that `IMG_1__3.jpg`.
    """
    root, conn = _lib(tmp_path, [
        ("one/IMG_1.jpg", "a" * 64, 1, None, 0),
        ("two/IMG_1.jpg", "b" * 64, 1, None, 0),
    ])
    conn.commit()
    dest = tmp_path / "views"
    rows, _s = arrange.build_plan(root, dest, "person")
    assert len(rows) == 2, rows

    for i in range(3):
        arrange.apply_plan(rows, tmp_path / f"l{i}.csv", dest=dest, prune=True)
        names = sorted(p.name for p in (dest / "Anna").iterdir())
        assert names == ["IMG_1.jpg", "IMG_1__1.jpg"], f"run {i + 1}: {names}"


def test_a_link_that_will_not_delete_is_named_not_just_counted(tmp_path, monkeypatch):
    """`failed: 1` tells the operator nothing about which file, or why.

    The real case: one photograph in `Albums/Products` is marked read-only.
    NTFS keeps that attribute once per file and every hard link shares it, so
    the view link cannot be unlinked — and clearing the flag to tidy a view
    would silently modify the original. Leaving it is right; saying so is
    the part that was missing.
    """
    from pathlib import Path

    root, _ = _lib(tmp_path, [("event/a.jpg", "a" * 64, 1, None, 0)])
    log = tmp_path / "log.csv"
    rows, _s = arrange.build_plan(root, tmp_path / "views", "person")
    arrange.apply_plan(rows, log)

    real_unlink = Path.unlink

    def refuse(self, *a, **kw):
        if self.suffix == ".jpg":
            raise PermissionError(5, "Access is denied")
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", refuse)
    stats = arrange.remove(log)
    assert stats["failed"] == 1 and stats["unlinked"] == 0
    assert len(stats["failures"]) == 1
    assert "a.jpg" in stats["failures"][0], stats["failures"]


def test_unsafe_characters_do_not_escape_the_destination(tmp_path):
    """A person named with a path separator must not write outside `dest`."""
    root, conn = _lib(tmp_path, [("a.jpg", "a" * 64, 1, None, 0)])
    conn.execute("UPDATE people SET name=? WHERE person_id=1", ("../../evil",))
    conn.commit()
    dest = tmp_path / "views"
    rows, _s = arrange.build_plan(root, dest, "person")
    for r in rows:
        assert dest in type(dest)(r["dst"]).parents, f"escaped dest: {r['dst']}"


def test_unknown_facet_is_rejected(tmp_path):
    root, _ = _lib(tmp_path, [("a.jpg", "a" * 64, 1, None, 0)])
    with pytest.raises(ValueError):
        arrange.build_plan(root, tmp_path / "views", "shoesize")
