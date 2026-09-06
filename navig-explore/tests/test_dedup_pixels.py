"""Tests for the identical-pixels duplicate class.

The case that motivates it: re-saving a JPEG, or a tool writing one EXIF tag, changes
every byte of the file while leaving every pixel untouched. SHA-256 sees two unrelated
files; a human sees one photo twice. On a real family album this was 184 of 210 name
collisions.
"""
from __future__ import annotations

import json

import pytest

from navig_explore import dedup

Image = pytest.importorskip("PIL.Image", reason="Pillow required")
# NOT importorskip("imagehash"): identical-pixel detection must work with a decoder
# alone. imagehash is absent on some machines here, and requiring it would let this
# whole class of duplicate silently go undetected while the tests still looked green.
_HAS_IMAGEHASH = True
try:
    import imagehash  # noqa: F401
except Exception:
    _HAS_IMAGEHASH = False


def _img(path, colour=(10, 120, 200), size=(64, 48), **save_kw):
    im = Image.new("RGB", size, colour)
    # a little structure so phash is not degenerate across different images
    for x in range(0, size[0], 8):
        for y in range(0, size[1], 8):
            im.putpixel((x, y), (255 - colour[0], colour[1] // 2, colour[2] // 3))
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, **save_kw)
    return path


def _recs(*paths):
    out = []
    for p in paths:
        out.append({"rel": p.name, "_abs": str(p), "name": p.name, "type": "image",
                    "size": p.stat().st_size, "w": 64, "h": 48})
    return out


def _log(*a):
    pass


def test_same_pixels_different_bytes_is_detected(tmp_path):
    """The core case byte-hashing structurally cannot see."""
    a = _img(tmp_path / "a.jpg", quality=95)
    b = _img(tmp_path / "b.jpg", quality=95, comment=b"a differing metadata comment")
    assert a.read_bytes() != b.read_bytes(), "fixture must differ in BYTES"

    groups = dedup.find_image_dupes(_recs(a, b), 6, _log, workers=2)
    px = [g for g in groups if g["kind"] == "identical-pixels"]
    assert len(px) == 1
    assert set(px[0]["members"]) == {"a.jpg", "b.jpg"}
    assert len(px[0]["auto_trash"]) == 1
    assert px[0]["keep"] not in px[0]["auto_trash"]


def test_the_copy_with_more_metadata_is_the_one_kept(tmp_path):
    """Pixels are equal, so the file carrying more metadata is the better original."""
    lean = _img(tmp_path / "lean.jpg", quality=95)
    rich = _img(tmp_path / "rich.jpg", quality=95, comment=b"x" * 4000)
    assert rich.stat().st_size > lean.stat().st_size

    groups = dedup.find_image_dupes(_recs(lean, rich), 6, _log, workers=2)
    px = [g for g in groups if g["kind"] == "identical-pixels"][0]
    assert px["keep"] == "rich.jpg"
    assert px["auto_trash"] == ["lean.jpg"]


def test_genuinely_different_images_are_not_pixel_duplicates(tmp_path):
    a = _img(tmp_path / "a.jpg", colour=(10, 120, 200))
    b = _img(tmp_path / "b.jpg", colour=(200, 30, 40))
    groups = dedup.find_image_dupes(_recs(a, b), 6, _log, workers=2)
    assert not [g for g in groups if g["kind"] == "identical-pixels"]


def test_a_resize_is_near_not_identical(tmp_path):
    """A resize has different pixels — it must stay FLAG-ONLY, never auto-trashed."""
    a = _img(tmp_path / "a.jpg", size=(128, 96), quality=95)
    b = _img(tmp_path / "b.jpg", size=(64, 48), quality=95)
    groups = dedup.find_image_dupes(_recs(a, b), 6, _log, workers=2)
    assert not [g for g in groups if g["kind"] == "identical-pixels"]
    for g in groups:
        assert "auto_trash" not in g, "a near-image cluster must not be auto-trashable"


def test_identical_pixels_works_without_imagehash(tmp_path, monkeypatch):
    """The decoder alone must be enough. Simulates the machine where imagehash is
    absent: perceptual clustering goes quiet, identical-pixel detection does not."""
    import builtins
    real_import = builtins.__import__

    def _no_imagehash(name, *a, **kw):
        if name == "imagehash":
            raise ImportError("simulated: imagehash not installed")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _no_imagehash)
    a = _img(tmp_path / "a.jpg", quality=95)
    b = _img(tmp_path / "b.jpg", quality=95, comment=b"metadata only")
    groups = dedup.find_image_dupes(_recs(a, b), 6, _log, workers=2)
    px = [g for g in groups if g["kind"] == "identical-pixels"]
    assert len(px) == 1, "identical-pixels must not depend on imagehash"
    assert not [g for g in groups if g["kind"] == "near-image"]


def test_an_undecodable_file_is_never_called_a_duplicate(tmp_path):
    """Unreadable must not mean redundant — that would quarantine unknown content."""
    good = _img(tmp_path / "good.jpg")
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"this is not a JPEG at all")
    bad2 = tmp_path / "bad2.jpg"
    bad2.write_bytes(b"this is not a JPEG at all")     # byte-identical garbage

    groups = dedup.find_image_dupes(_recs(good, bad, bad2), 6, _log, workers=2)
    flagged = {m for g in groups for m in g["members"]}
    assert "bad.jpg" not in flagged
    assert "bad2.jpg" not in flagged


def test_files_already_claimed_by_the_exact_pass_are_not_restated(tmp_path):
    a = _img(tmp_path / "a.jpg", quality=95)
    b = _img(tmp_path / "b.jpg", quality=95, comment=b"different")
    groups = dedup.find_image_dupes(_recs(a, b), 6, _log, workers=2,
                                    already_trashed={"b.jpg"})
    assert not [g for g in groups if g["kind"] == "identical-pixels"]


def test_dedup_writes_identical_pixels_and_photos_consumes_them(tmp_path):
    """End-to-end: dedup -> dupes.jsonl -> photos._load_autotrash."""
    root = tmp_path / "lib"
    a = _img(root / "a.jpg", quality=95)
    b = _img(root / "b.jpg", quality=95, comment=b"metadata only")
    side = root / ".mediaexplorer"
    side.mkdir(parents=True, exist_ok=True)
    with (side / "meta.jsonl").open("w", encoding="utf-8") as f:
        for p in (a, b):
            f.write(json.dumps({"rel": p.name, "abs": str(p), "name": p.name,
                                "type": "image", "size": p.stat().st_size,
                                "w": 64, "h": 48}) + "\n")

    summary = dedup.dedup([root], quiet=True)
    assert summary["identical_pixel_groups"] == 1
    assert summary["identical_pixel_files"] == 1

    from navig_explore import photos as ph
    assert len(ph._load_autotrash(root, pixel_dupes=True)) == 1
    assert ph._load_autotrash(root, pixel_dupes=False) == set(), \
        "--no-pixel-dupes must fall back to byte-identical copies only"


def test_exact_only_skips_the_decode_pass_entirely(tmp_path):
    root = tmp_path / "lib"
    a = _img(root / "a.jpg", quality=95)
    b = _img(root / "b.jpg", quality=95, comment=b"metadata only")
    side = root / ".mediaexplorer"
    side.mkdir(parents=True, exist_ok=True)
    with (side / "meta.jsonl").open("w", encoding="utf-8") as f:
        for p in (a, b):
            f.write(json.dumps({"rel": p.name, "abs": str(p), "name": p.name,
                                "type": "image", "size": p.stat().st_size}) + "\n")

    summary = dedup.dedup([root], quiet=True, exact_only=True)
    assert summary["identical_pixel_groups"] == 0
    assert summary["near_image_clusters"] == 0
