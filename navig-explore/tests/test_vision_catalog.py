"""Catalog invariants: content-keying, idempotence, and reversible triage.

The first test here is the load-bearing one. Path-keyed metadata is why every
off-the-shelf photo manager loses your person names when you reorganise a
library — and this package's own output is a reorganisation plan.
"""
from __future__ import annotations

import json
from pathlib import Path

from navig_explore.vision import catalog, triage


def _write_meta(root, rows):
    side = root / ".mediaexplorer"
    side.mkdir(parents=True, exist_ok=True)
    with (side / "meta.jsonl").open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _row(root, rel, *, size=1000, mtime=1531405800.0, **kw):
    d = {"rel": rel, "abs": str(root / rel), "name": rel.split("/")[-1],
         "ext": ".jpg", "type": "image", "size": size, "mtime": mtime,
         "probe_ok": True, "w": 800, "h": 600, "created": "", "camera": "",
         "gps": None, "res_tier": "SD", "source_class": "unknown"}
    d.update(kw)
    return d


def test_labels_survive_a_file_moving(tmp_path):
    """The whole reason the catalog is keyed on sha256 rather than path.

    A photo is indexed, gets a face and a date, then the library is reorganised
    and the file appears at a new path. Every derived fact must still be found.
    """
    root = tmp_path / "lib"
    root.mkdir()
    _write_meta(root, [_row(root, "inbox/a.jpg")])
    conn = catalog.connect(root)
    catalog.ingest_meta_jsonl(conn, root, quiet=True)

    sha = "deadbeef" * 8
    conn.execute("UPDATE files SET sha256=? WHERE rel='inbox/a.jpg'", (sha,))
    conn.execute("INSERT INTO assets (sha256, decoded, w, h) VALUES (?,1,800,600)", (sha,))
    conn.execute("INSERT INTO people (person_id, name, locked) VALUES (7,'Anna',1)")
    conn.execute("""INSERT INTO faces (sha256, x, y, w, h, score, rotation, engine,
                                       person_id) VALUES (?,0.1,0.1,0.2,0.2,0.9,0,'t',7)""",
                 (sha,))
    conn.execute("""INSERT INTO dates (sha256, value, source, confidence)
                    VALUES (?, '2005-02-15T12:00:00', 'overlay', 0.95)""", (sha,))
    conn.commit()

    # …the library is reorganised: same bytes, new path.
    _write_meta(root, [_row(root, "By Date/2005/2005-02/a.jpg")])
    catalog.ingest_meta_jsonl(conn, root, quiet=True)
    conn.execute("UPDATE files SET sha256=? WHERE rel='By Date/2005/2005-02/a.jpg'", (sha,))
    conn.commit()

    moved = conn.execute(
        """SELECT d.value, p.name FROM files f
           JOIN dates  d ON d.sha256 = f.sha256
           JOIN faces  fa ON fa.sha256 = f.sha256
           JOIN people p ON p.person_id = fa.person_id
           WHERE f.rel = 'By Date/2005/2005-02/a.jpg'""").fetchone()
    assert moved is not None, "derived facts were lost when the file moved"
    assert moved["value"].startswith("2005-02-15")
    assert moved["name"] == "Anna"


def test_ingest_is_idempotent(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    _write_meta(root, [_row(root, "a.jpg"), _row(root, "b.jpg")])
    conn = catalog.connect(root)
    first = catalog.ingest_meta_jsonl(conn, root, quiet=True)
    second = catalog.ingest_meta_jsonl(conn, root, quiet=True)
    assert first["inserted"] == 2
    assert second["inserted"] == 0 and second["updated"] == 2
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 2


def test_reingest_does_not_revive_a_file_the_pipeline_found_missing(tmp_path):
    """Regression: ingest reset present=1 and re-queued 22,669 phantom files.

    `meta.jsonl` is append-only history and can be stale; `present` reflects what
    the decode pass actually found on disk. The sidecar must not overrule it, or
    every run re-walks files that have already been established as gone.
    """
    root = tmp_path / "lib"
    root.mkdir()
    _write_meta(root, [_row(root, "gone.jpg")])
    conn = catalog.connect(root)
    catalog.ingest_meta_jsonl(conn, root, quiet=True)

    conn.execute("UPDATE files SET present=0")     # the pipeline saw it vanish
    conn.commit()
    catalog.ingest_meta_jsonl(conn, root, quiet=True)   # same stale sidecar again

    assert conn.execute("SELECT present FROM files").fetchone()[0] == 0
    assert list(catalog.iter_pending(conn, root)) == [], \
        "a known-missing file was re-queued for indexing"


def test_reingest_revives_a_file_that_genuinely_came_back(tmp_path):
    """A changed size/mtime IS a re-observation, so the row must come back."""
    root = tmp_path / "lib"
    root.mkdir()
    _write_meta(root, [_row(root, "back.jpg")])
    conn = catalog.connect(root)
    catalog.ingest_meta_jsonl(conn, root, quiet=True)
    conn.execute("UPDATE files SET present=0")
    conn.commit()

    _write_meta(root, [_row(root, "back.jpg", size=4242, mtime=1600000000.0)])
    catalog.ingest_meta_jsonl(conn, root, quiet=True)
    assert conn.execute("SELECT present FROM files").fetchone()[0] == 1


def test_reingest_keeps_hash_when_bytes_cannot_have_changed(tmp_path):
    """Re-running probe must not force a re-hash of an unchanged library."""
    root = tmp_path / "lib"
    root.mkdir()
    _write_meta(root, [_row(root, "a.jpg")])
    conn = catalog.connect(root)
    catalog.ingest_meta_jsonl(conn, root, quiet=True)
    conn.execute("UPDATE files SET sha256='abc'")
    conn.commit()
    catalog.ingest_meta_jsonl(conn, root, quiet=True)
    assert conn.execute("SELECT sha256 FROM files").fetchone()[0] == "abc"


def test_reingest_clears_hash_when_the_file_changed(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    _write_meta(root, [_row(root, "a.jpg")])
    conn = catalog.connect(root)
    catalog.ingest_meta_jsonl(conn, root, quiet=True)
    conn.execute("UPDATE files SET sha256='abc'")
    conn.commit()
    _write_meta(root, [_row(root, "a.jpg", size=2222)])   # edited in place
    catalog.ingest_meta_jsonl(conn, root, quiet=True)
    assert conn.execute("SELECT sha256 FROM files").fetchone()[0] is None


def test_vectors_round_trip_as_unit_vectors(tmp_path):
    import numpy as np

    v = np.array([3.0, 4.0, 0.0, 0.0], dtype="float32")
    back = catalog.unpack_vec(catalog.pack_vec(v))
    assert abs(float(np.linalg.norm(back)) - 1.0) < 1e-2
    assert abs(float(back[0]) - 0.6) < 1e-2


# ── triage is reversible, and never overwrites ──────────────────────────────
def test_apply_then_undo_restores_every_file(tmp_path):
    root = tmp_path / "lib"
    (root / "src").mkdir(parents=True)
    f = root / "src" / "shot.jpg"
    f.write_bytes(b"pixels")
    rows = [{"action": "triage-screenshot", "src": str(f),
             "dst": str(root / "_screenshots" / "shot.jpg"), "why": "test"}]

    log = root / "log.csv"
    triage.apply_plan(rows, log)
    assert not f.exists()
    assert (root / "_screenshots" / "shot.jpg").read_bytes() == b"pixels"

    triage.undo(log)
    assert f.read_bytes() == b"pixels"


def test_apply_repoints_the_catalog_at_the_new_paths(tmp_path):
    """Regression: triage moved 2,712 files and never told the index.

    Every downstream command — search, gallery, face crops — then dereferenced
    paths this same operation had just emptied. Because rows are keyed on content
    hash, repointing the path is enough for the person names and dates hanging
    off that hash to follow.
    """
    root = tmp_path / "lib"
    root.mkdir()
    _write_meta(root, [_row(root, "shot.jpg")])
    conn = catalog.connect(root)
    catalog.ingest_meta_jsonl(conn, root, quiet=True)
    sha = "f" * 64
    conn.execute("UPDATE files SET sha256=?", (sha,))
    conn.execute("INSERT INTO assets (sha256, decoded) VALUES (?,1)", (sha,))
    conn.execute("INSERT INTO people (person_id, name, locked) VALUES (3,'Bea',1)")
    conn.execute("""INSERT INTO faces (sha256,x,y,w,h,score,rotation,engine,person_id)
                    VALUES (?,0,0,1,1,0.9,0,'t',3)""", (sha,))
    conn.commit()

    src = root / "shot.jpg"
    src.write_bytes(b"pixels")
    dst = root / "_webcam" / "shot.jpg"
    stats = triage.apply_plan(
        [{"action": "triage-webcam", "src": str(src), "dst": str(dst), "why": "t"}],
        root / "log.csv", conn=conn)

    assert stats["catalog_repointed"] == 1
    row = conn.execute("""SELECT f.path, f.rel, p.name FROM files f
                          JOIN faces fa ON fa.sha256 = f.sha256
                          JOIN people p ON p.person_id = fa.person_id""").fetchone()
    assert row["rel"] == "_webcam/shot.jpg"
    assert Path(row["path"]).exists(), "catalog points at a path that does not exist"
    assert row["name"] == "Bea", "person name did not survive the move"


def test_undo_repoints_the_catalog_back(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    _write_meta(root, [_row(root, "shot.jpg")])
    conn = catalog.connect(root)
    catalog.ingest_meta_jsonl(conn, root, quiet=True)
    conn.execute("UPDATE files SET sha256='e'")
    conn.commit()

    src = root / "shot.jpg"
    src.write_bytes(b"pixels")
    log = root / "log.csv"
    triage.apply_plan([{"action": "triage-web", "src": str(src),
                        "dst": str(root / "_web" / "shot.jpg"), "why": "t"}],
                      log, conn=conn)
    triage.undo(log, conn=conn)

    row = conn.execute("SELECT path, rel FROM files").fetchone()
    assert row["rel"] == "shot.jpg"
    assert Path(row["path"]).exists()


def test_collision_never_overwrites(tmp_path):
    root = tmp_path / "lib"
    (root / "a").mkdir(parents=True)
    (root / "_web").mkdir(parents=True)
    src = root / "a" / "x.jpg"
    src.write_bytes(b"new")
    existing = root / "_web" / "x.jpg"
    existing.write_bytes(b"original")

    triage.apply_plan(
        [{"action": "triage-web-graphic", "src": str(src),
          "dst": str(existing), "why": "t"}], root / "log.csv")
    assert existing.read_bytes() == b"original", "an existing file was overwritten"
    assert (root / "_web" / "x__1.jpg").read_bytes() == b"new"


def test_plan_never_moves_a_photograph(tmp_path):
    """Where a real photo belongs is the operator's call, not the classifier's."""
    root = tmp_path / "lib"
    root.mkdir()
    _write_meta(root, [_row(root, "a.jpg")])
    conn = catalog.connect(root)
    catalog.ingest_meta_jsonl(conn, root, quiet=True)
    sha = "a" * 64
    conn.execute("UPDATE files SET sha256=?", (sha,))
    conn.execute("INSERT INTO assets (sha256, decoded, w, h) VALUES (?,1,800,600)", (sha,))
    conn.execute("INSERT INTO classes (sha256, class, score) VALUES (?,'photo',0.5)", (sha,))
    conn.commit()

    rows, summary = triage.build_plan(root)
    assert rows == []
    assert summary.get("keep") == 1


def test_weakly_dated_photos_are_not_refiled(tmp_path):
    """A folder-year guess must never be grounds to move a file into the date tree."""
    root = tmp_path / "lib"
    root.mkdir()
    _write_meta(root, [_row(root, "a.jpg")])
    conn = catalog.connect(root)
    catalog.ingest_meta_jsonl(conn, root, quiet=True)
    sha = "b" * 64
    conn.execute("UPDATE files SET sha256=?", (sha,))
    conn.execute("INSERT INTO assets (sha256, decoded, w, h) VALUES (?,1,800,600)", (sha,))
    conn.execute("INSERT INTO classes (sha256, class, score) VALUES (?,'photo',0.5)", (sha,))
    conn.execute("""INSERT INTO dates (sha256, value, source, confidence)
                    VALUES (?, '2009-07-01T12:00:00', 'folder', 0.6)""", (sha,))
    conn.commit()

    rows, summary = triage.build_plan(root, refile_dated=True)
    assert rows == []
    assert summary.get("date-too-weak-to-move") == 1


def test_strongly_dated_photo_is_refiled(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    _write_meta(root, [_row(root, "a.jpg")])
    conn = catalog.connect(root)
    catalog.ingest_meta_jsonl(conn, root, quiet=True)
    sha = "c" * 64
    conn.execute("UPDATE files SET sha256=?", (sha,))
    conn.execute("INSERT INTO assets (sha256, decoded, w, h) VALUES (?,1,800,600)", (sha,))
    conn.execute("INSERT INTO classes (sha256, class, score) VALUES (?,'photo',0.5)", (sha,))
    conn.execute("""INSERT INTO dates (sha256, value, source, confidence)
                    VALUES (?, '2005-02-15T12:00:00', 'overlay', 0.95)""", (sha,))
    conn.commit()

    rows, _ = triage.build_plan(root, refile_dated=True)
    assert len(rows) == 1
    assert rows[0]["action"] == "refile-dated"
    assert "2005" in rows[0]["dst"] and "2005-02" in rows[0]["dst"]


def test_missing_and_undecodable_are_counted_separately(tmp_path):
    """A stale sidecar must not make healthy photos look corrupt.

    Regression: a full-library run reported 28,001 "undecodable" files. 22,669 of
    them were perfectly good photos that had simply MOVED since `probe` last ran —
    meta.jsonl predated a reorganisation. Summing "gone" into "corrupt" turned a
    routine staleness notice into a fake data-loss report.
    """
    from navig_explore.vision import pipeline

    root = tmp_path / "lib"
    root.mkdir()
    good = root / "real.jpg"
    from PIL import Image
    Image.new("RGB", (32, 32), "red").save(good)
    broken = root / "broken.jpg"
    broken.write_bytes(b"not an image at all")

    rec_ok = pipeline._decode_one(str(good), None)
    rec_bad = pipeline._decode_one(str(broken), None)
    rec_gone = pipeline._decode_one(str(root / "vanished.jpg"), None)

    assert rec_ok.ok and not rec_ok.missing and rec_ok.sha256
    assert not rec_bad.ok and not rec_bad.missing, "corrupt bytes are not 'missing'"
    assert rec_bad.sha256, "a corrupt file still has content worth hashing"
    assert rec_gone.missing and not rec_gone.ok, "a vanished file is not 'undecodable'"
    assert rec_gone.sha256 is None
    assert "missing" in rec_gone.reason


def test_undecodable_files_are_named_not_skipped(tmp_path):
    """5,664 files in the real library are bad recovery bytes. Silence would read
    as "clean"."""
    root = tmp_path / "lib"
    root.mkdir()
    _write_meta(root, [_row(root, "broken.jpg")])
    conn = catalog.connect(root)
    catalog.ingest_meta_jsonl(conn, root, quiet=True)
    sha = "d" * 64
    conn.execute("UPDATE files SET sha256=?", (sha,))
    conn.execute("""INSERT INTO assets (sha256, decoded, fail_reason)
                    VALUES (?,0,'decode: UnidentifiedImageError')""", (sha,))
    conn.commit()

    rows, summary = triage.build_plan(root)
    assert summary.get("undecodable") == 1
    assert rows[0]["action"] == "quarantine-undecodable"
