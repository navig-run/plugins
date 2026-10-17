"""Regression tests for the navig-dedupe engines — pure, no navig, no external binaries.

Covers the invariants that matter: byte-duplicates group, perceptual copies cluster,
redundant thumbnails are detected, and quarantine MOVES (never deletes).
"""
from __future__ import annotations

import shutil

import numpy as np
from PIL import Image


def _gradient(w: int = 64, h: int = 64) -> np.ndarray:
    return np.tile(np.linspace(0, 255, w).astype("uint8"), (h, 1))


def _save(path, arr: np.ndarray) -> None:
    Image.fromarray(arr.astype("uint8"), "L").save(path)


# ── file (exact SHA-256) ──────────────────────────────────────────────────────

def test_file_exact_duplicates_group(tmp_path):
    from navig_dedupe import file as fd

    (tmp_path / "x.bin").write_bytes(b"hello world")
    (tmp_path / "y.bin").write_bytes(b"hello world")   # identical bytes
    (tmp_path / "z.bin").write_bytes(b"different")

    clusters = fd.cluster(fd.hash_dir(tmp_path))
    assert any({"x.bin", "y.bin"} <= set(g) for g in clusters)
    assert not any("z.bin" in g for g in clusters)


def test_file_recursive(tmp_path):
    from navig_dedupe import file as fd

    (tmp_path / "a.bin").write_bytes(b"dup")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.bin").write_bytes(b"dup")

    flat = fd.cluster(fd.hash_dir(tmp_path, recursive=False))
    assert flat == []                                   # b.bin hidden without recursion
    deep = fd.cluster(fd.hash_dir(tmp_path, recursive=True))
    assert any(len(g) == 2 for g in deep)               # a.bin + sub/b.bin


# ── image (perceptual dHash) ──────────────────────────────────────────────────

def test_image_exact_copy_clusters(tmp_path):
    from navig_dedupe import image as im

    _save(tmp_path / "a.png", _gradient())
    shutil.copy(tmp_path / "a.png", tmp_path / "a_copy.png")   # byte-identical

    clusters = im.cluster(im.hash_dir(tmp_path), threshold=0)
    assert any({"a.png", "a_copy.png"} <= set(g) for g in clusters)


def test_image_different_does_not_cluster(tmp_path):
    from navig_dedupe import image as im

    _save(tmp_path / "a.png", _gradient())
    _save(tmp_path / "b.png", _gradient()[:, ::-1])           # reversed gradient → opposite dHash

    clusters = im.cluster(im.hash_dir(tmp_path), threshold=0)
    assert not any({"a.png", "b.png"} <= set(g) for g in clusters)


def test_image_resized_clusters_near(tmp_path):
    from navig_dedupe import image as im

    _save(tmp_path / "a.png", _gradient(128, 128))
    Image.open(tmp_path / "a.png").resize((48, 48)).save(tmp_path / "a_small.png")

    near = im.cluster(im.hash_dir(tmp_path), threshold=12)     # --near band
    assert any({"a.png", "a_small.png"} <= set(g) for g in near)


def test_redundant_thumbs(tmp_path):
    from navig_dedupe import image as im

    (tmp_path / "clip.mp4_thumb.jpg").write_bytes(b"")         # poster for a video → redundant
    (tmp_path / "photo.jpg").write_bytes(b"")
    (tmp_path / "photo.jpg_thumb.jpg").write_bytes(b"")        # thumb of a kept full image

    thumbs = set(im.redundant_thumbs(tmp_path))
    assert "clip.mp4_thumb.jpg" in thumbs
    assert "photo.jpg_thumb.jpg" in thumbs
    assert "photo.jpg" not in thumbs                          # the real file is kept


# ── quarantine: MOVE, never DELETE ────────────────────────────────────────────

def test_quarantine_moves_not_deletes(tmp_path):
    from navig_dedupe import image as im

    _save(tmp_path / "keep.png", _gradient())
    _save(tmp_path / "dup.png", _gradient())
    qdir = tmp_path / "_q"

    res = im.quarantine(tmp_path, ["dup.png"], qdir)

    assert res["quarantined"] == 1
    assert not (tmp_path / "dup.png").exists()                # gone from source
    assert (qdir / "dup.png").exists()                        # MOVED, not deleted
    assert (tmp_path / "keep.png").exists()                   # kept file untouched


def test_extras_to_drop_keeps_largest(tmp_path):
    from navig_dedupe import image as im

    (tmp_path / "big.png").write_bytes(b"x" * 5000)
    (tmp_path / "small.png").write_bytes(b"x" * 10)

    drop = im.extras_to_drop(tmp_path, [["big.png", "small.png"]])
    assert drop == ["small.png"]                              # largest kept
