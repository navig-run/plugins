"""`route.mirror_apply` must not enumerate the source tree while emptying it.

`_do_migrate` moves each source into the trash — deleting entries from the very
directories `os.walk` is enumerating. Directory enumeration is not stable under
concurrent modification (on Windows it is FindFirstFile/FindNextFile, which silently
skips entries when the directory changes mid-scan), so interleaving the two drops files
from the mirror with no error and no count.

This is the same defect class that orphaned 28 `.md` sidecars in audio_sort's `apply`.
"""
from __future__ import annotations

import os
from pathlib import Path

from navig_explore import route


def _tree(root: Path, n: int) -> None:
    for i in range(n):
        d = root / f"sub{i % 3}"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"f{i}.bin").write_bytes(f"payload {i}".encode())


def test_walk_completes_before_any_file_is_migrated(tmp_path, monkeypatch):
    """The invariant that makes the race impossible: enumerate fully, then move."""
    src, dst, trash = tmp_path / "src", tmp_path / "dst", tmp_path / "trash"
    _tree(src, 12)

    walk_finished = {"done": False}
    real_walk = os.walk

    def tracking_walk(*a, **kw):
        yield from real_walk(*a, **kw)
        walk_finished["done"] = True

    migrated = []

    def fake_migrate(s, d, t, verify, st):
        assert walk_finished["done"], "migrated a file while the walk was still running"
        migrated.append(s)
        st["trashed"] += 1

    monkeypatch.setattr(route.os, "walk", tracking_walk)
    monkeypatch.setattr(route, "_do_migrate", fake_migrate)
    route.mirror_apply(src, dst, trash, quiet=True)
    assert len(migrated) == 12


def test_every_file_is_mirrored_and_the_source_tree_is_emptied(tmp_path):
    src, dst, trash = tmp_path / "src", tmp_path / "dst", tmp_path / "trash"
    _tree(src, 30)
    st = route.mirror_apply(src, dst, trash, verify=False, quiet=True)

    assert st["error"] == 0
    mirrored = sorted(p.name for p in dst.rglob("*") if p.is_file())
    assert len(mirrored) == 30, f"only {len(mirrored)} of 30 reached the mirror"
    assert not [p for p in src.rglob("*") if p.is_file()], "sources left behind"
    trashed = [p for p in trash.rglob("*") if p.is_file()]
    assert len(trashed) == 30, "originals must be retained in trash, never deleted"


def test_limit_stops_early_without_losing_the_rest(tmp_path):
    src, dst, trash = tmp_path / "src", tmp_path / "dst", tmp_path / "trash"
    _tree(src, 20)
    st = route.mirror_apply(src, dst, trash, verify=False, quiet=True, limit=5)
    assert st["trashed"] == 5
    assert len([p for p in src.rglob("*") if p.is_file()]) == 15
