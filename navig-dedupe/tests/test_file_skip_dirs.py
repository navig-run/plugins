"""A dedupe scan must not report its own quarantine back as duplicates.

`_finalize` already strips the quarantine dir of the CURRENT run (derived from `--move`).
It knows nothing about the ones left by PREVIOUS runs — and after the first `--move`,
every file in `.trash/` is by construction byte-identical to the original it was
quarantined for. Scanning the parent then reports all of them as fresh duplicates, i.e.
"quarantine these" about files that already are, and worse, can nominate the quarantined
copy as the keeper and push the live original out.

Skipping quarantine-shaped directories is therefore a correctness fix, not a preference.
"""
from __future__ import annotations

import os

from navig_dedupe import file as fd


def _w(p, data: bytes):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def test_a_previous_runs_quarantine_is_not_reported_as_duplicates(tmp_path):
    """The exact scenario: last week's --move left copies in .trash/."""
    _w(tmp_path / "library" / "song.mp3", b"the song")
    _w(tmp_path / ".trash" / "dedupe-2026" / "song.mp3", b"the song")
    assert fd.cluster(fd.hash_dir(tmp_path, recursive=True)) == []


def test_every_default_quarantine_name_is_skipped(tmp_path):
    _w(tmp_path / "keep.bin", b"payload")
    for name in ("_dupes", "_duplicates", "_quarantine", "_trash", ".trash"):
        _w(tmp_path / name / "copy.bin", b"payload")
    assert fd.cluster(fd.hash_dir(tmp_path, recursive=True)) == []


def test_real_duplicates_outside_quarantine_are_still_found(tmp_path):
    """The skip must not become a blindfold."""
    _w(tmp_path / "a" / "song.mp3", b"the song")
    _w(tmp_path / "b" / "song.mp3", b"the song")
    _w(tmp_path / ".trash" / "song.mp3", b"the song")
    clusters = fd.cluster(fd.hash_dir(tmp_path, recursive=True))
    assert len(clusters) == 1
    assert sorted(clusters[0]) == [os.path.join("a", "song.mp3"),
                                   os.path.join("b", "song.mp3")]


def test_pointing_the_root_at_a_quarantine_still_scans_it(tmp_path):
    """Only nested quarantine dirs are skipped — deduping the trash itself is valid."""
    trash = tmp_path / ".trash"
    _w(trash / "a.bin", b"same")
    _w(trash / "b.bin", b"same")
    clusters = fd.cluster(fd.hash_dir(trash, recursive=True))
    assert len(clusters) == 1 and len(clusters[0]) == 2


def test_explicit_skip_overrides_the_default(tmp_path):
    _w(tmp_path / "keep" / "x.bin", b"same")
    _w(tmp_path / ".trash" / "x.bin", b"same")
    both = fd.cluster(fd.hash_dir(tmp_path, recursive=True, skip=set()))
    assert len(both) == 1 and len(both[0]) == 2


def test_caller_can_add_its_own_skipped_names(tmp_path):
    _w(tmp_path / "keep" / "x.bin", b"same")
    _w(tmp_path / "node_modules" / "x.bin", b"same")
    assert fd.cluster(fd.hash_dir(tmp_path, recursive=True)) != []
    skip = set(fd.DEFAULT_SKIP_DIRS) | {"node_modules"}
    assert fd.cluster(fd.hash_dir(tmp_path, recursive=True, skip=skip)) == []


def test_a_skipped_directory_is_never_descended_into(tmp_path, monkeypatch):
    """Pruning, not filtering — on a 4 TB drive the traversal itself is the cost."""
    _w(tmp_path / "keep.bin", b"x")
    deep = tmp_path / ".trash" / "a" / "b" / "c"
    _w(deep / "buried.bin", b"x")

    walked: list[str] = []
    real_walk = os.walk

    def tracking_walk(top, *a, **kw):
        for dirpath, dirnames, filenames in real_walk(top, *a, **kw):
            walked.append(dirpath)
            yield dirpath, dirnames, filenames

    monkeypatch.setattr(fd.os, "walk", tracking_walk)
    fd.hash_dir(tmp_path, recursive=True)
    assert not [w for w in walked if ".trash" in w], f"descended into: {walked}"


def test_non_recursive_scan_is_unaffected(tmp_path):
    _w(tmp_path / "a.bin", b"same")
    _w(tmp_path / "b.bin", b"same")
    _w(tmp_path / ".trash" / "c.bin", b"same")
    clusters = fd.cluster(fd.hash_dir(tmp_path, recursive=False))
    assert len(clusters) == 1 and sorted(clusters[0]) == ["a.bin", "b.bin"]
