"""The size/head sieves must be a pure optimisation: same clusters, far less reading.

`hash_dir` used to open and hash every file in the tree. The sieves are only worth having
if they are *exact* — identical files always share a size and a head, so a correct sieve
can never drop a real duplicate. These tests pin both halves: the clusters are unchanged,
and the files that cannot possibly cluster are never read.
"""
from __future__ import annotations

import hashlib
import os

from navig_dedupe import file as fd


def _w(p, data: bytes):
    p.write_bytes(data)
    return p


def test_finds_exact_duplicates(tmp_path):
    _w(tmp_path / "a.bin", b"same content")
    _w(tmp_path / "b.bin", b"same content")
    _w(tmp_path / "c.bin", b"different")
    clusters = fd.cluster(fd.hash_dir(tmp_path))
    assert clusters == [["a.bin", "b.bin"]]


def test_same_size_different_content_is_not_a_duplicate(tmp_path):
    """The size sieve must not be mistaken for an answer."""
    _w(tmp_path / "a.bin", b"AAAAAAAA")
    _w(tmp_path / "b.bin", b"BBBBBBBB")
    assert fd.cluster(fd.hash_dir(tmp_path)) == []


def test_files_identical_past_the_head_still_cluster(tmp_path):
    """Same size, same first 64 KB, differing later — must survive both sieves."""
    head = b"H" * fd.HEAD_BYTES
    _w(tmp_path / "a.bin", head + b"tail")
    _w(tmp_path / "b.bin", head + b"tail")
    _w(tmp_path / "c.bin", head + b"TAIL")
    clusters = fd.cluster(fd.hash_dir(tmp_path))
    assert clusters == [["a.bin", "b.bin"]]


def test_same_head_different_tail_is_not_a_duplicate(tmp_path):
    head = b"H" * fd.HEAD_BYTES
    _w(tmp_path / "a.bin", head + b"one")
    _w(tmp_path / "b.bin", head + b"two")
    assert fd.cluster(fd.hash_dir(tmp_path)) == []


def test_uniquely_sized_files_are_never_opened(tmp_path, monkeypatch):
    """The whole point: a file that cannot have a twin must cost one stat, not a read."""
    _w(tmp_path / "unique1.bin", b"x")
    _w(tmp_path / "unique2.bin", b"yy")
    _w(tmp_path / "dupe_a.bin", b"zzz")
    _w(tmp_path / "dupe_b.bin", b"zzz")

    opened: list[str] = []
    real_open = open

    def tracking_open(f, *a, **kw):
        opened.append(str(f))
        return real_open(f, *a, **kw)

    monkeypatch.setattr("builtins.open", tracking_open)
    clusters = fd.cluster(fd.hash_dir(tmp_path))

    assert clusters == [["dupe_a.bin", "dupe_b.bin"]]
    # compare basenames: pytest's tmp_path is itself named after this test
    names = {os.path.basename(o) for o in opened}
    assert not {n for n in names if n.startswith("unique")}, \
        f"read a uniquely-sized file: {sorted(names)}"


def test_a_small_file_is_read_once_not_twice(tmp_path, monkeypatch):
    """Under HEAD_BYTES the head hash *is* the full hash — re-reading is pure waste."""
    _w(tmp_path / "a.bin", b"tiny")
    _w(tmp_path / "b.bin", b"tiny")

    reads: list[str] = []
    real_open = open

    def tracking_open(f, *a, **kw):
        reads.append(str(f))
        return real_open(f, *a, **kw)

    monkeypatch.setattr("builtins.open", tracking_open)
    assert fd.cluster(fd.hash_dir(tmp_path)) == [["a.bin", "b.bin"]]
    assert reads.count(str(tmp_path / "a.bin")) == 1


def test_returned_hashes_are_real_full_content_hashes(tmp_path):
    """A head hash must never be handed back as if it were the file's hash."""
    payload = b"Q" * (fd.HEAD_BYTES + 500)
    _w(tmp_path / "a.bin", payload)
    _w(tmp_path / "b.bin", payload)
    hashes = fd.hash_dir(tmp_path)
    assert set(hashes.values()) == {hashlib.sha256(payload).hexdigest()}


def test_small_file_hash_is_also_the_full_content_hash(tmp_path):
    _w(tmp_path / "a.bin", b"tiny")
    _w(tmp_path / "b.bin", b"tiny")
    hashes = fd.hash_dir(tmp_path)
    assert set(hashes.values()) == {hashlib.sha256(b"tiny").hexdigest()}


def test_empty_files_cluster_together(tmp_path):
    _w(tmp_path / "a.bin", b"")
    _w(tmp_path / "b.bin", b"")
    assert fd.cluster(fd.hash_dir(tmp_path)) == [["a.bin", "b.bin"]]


def test_recursive_flag_still_controls_depth(tmp_path):
    (tmp_path / "sub").mkdir()
    _w(tmp_path / "a.bin", b"same")
    _w(tmp_path / "sub" / "b.bin", b"same")
    assert fd.cluster(fd.hash_dir(tmp_path, recursive=False)) == []
    deep = fd.cluster(fd.hash_dir(tmp_path, recursive=True))
    assert deep and len(deep[0]) == 2


def test_progress_reports_against_the_candidate_set(tmp_path):
    for i in range(4):
        _w(tmp_path / f"d{i}.bin", b"same payload")
    _w(tmp_path / "lonely.bin", b"unique size here")
    seen = []
    fd.hash_dir(tmp_path, progress=lambda done, total: seen.append((done, total)))
    assert seen, "progress was never called"
    done, total = seen[-1]
    assert total == 4 and done == 4      # the lonely file is not part of the work


def test_an_unreadable_file_does_not_abort_the_scan(tmp_path, monkeypatch):
    _w(tmp_path / "a.bin", b"same")
    _w(tmp_path / "b.bin", b"same")
    real_open = open

    def flaky(f, *a, **kw):
        if str(f).endswith("a.bin"):
            raise PermissionError("locked")
        return real_open(f, *a, **kw)

    monkeypatch.setattr("builtins.open", flaky)
    assert fd.hash_dir(tmp_path) == {} or "b.bin" in fd.hash_dir(tmp_path)
