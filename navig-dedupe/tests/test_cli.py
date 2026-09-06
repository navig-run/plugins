"""Smoke + behaviour tests for the standalone `navig-dedupe` / `navig dedupe` CLI.

Drives the same `dedupe_app` that navig mounts. Uses only the file (SHA-256) mode so
the tests need no external binaries (fpcalc/ffmpeg).
"""
from __future__ import annotations

import json
import shutil

import numpy as np
from PIL import Image
from typer.testing import CliRunner

from navig_dedupe.commands.dedupe import _MANIFEST, dedupe_app

runner = CliRunner()


def _two_dupes(tmp_path):
    (tmp_path / "x.bin").write_bytes(b"same-bytes")
    (tmp_path / "y.bin").write_bytes(b"same-bytes")
    (tmp_path / "u.bin").write_bytes(b"unique")


def _img(path):
    arr = np.tile(np.linspace(0, 255, 64).astype("uint8"), (64, 1))
    Image.fromarray(arr, "L").save(path)


def test_help_runs():
    r = runner.invoke(dedupe_app, ["--help"])
    assert r.exit_code == 0
    assert "scan" in r.stdout


def test_scan_dry_run_reports_without_touching(tmp_path):
    _two_dupes(tmp_path)
    r = runner.invoke(dedupe_app, ["scan", str(tmp_path), "--files"])
    assert r.exit_code == 0
    assert "reclaimable" in r.stdout.lower()
    # dry run — nothing moved
    assert (tmp_path / "x.bin").exists()
    assert (tmp_path / "y.bin").exists()


def test_scan_move_quarantines_one_keeps_one(tmp_path):
    _two_dupes(tmp_path)
    qdir = tmp_path / "_dupes"
    r = runner.invoke(dedupe_app, ["scan", str(tmp_path), "--files", "--move", str(qdir)])
    assert r.exit_code == 0
    assert "Quarantined" in r.stdout
    # exactly one of the identical pair remains in place; the other was MOVED (not deleted)
    remaining = sorted(p.name for p in tmp_path.iterdir() if p.is_file())
    assert remaining == ["u.bin", "x.bin"] or remaining == ["u.bin", "y.bin"]
    assert qdir.exists() and any(qdir.iterdir())


def test_scan_json_is_parseable(tmp_path):
    _two_dupes(tmp_path)
    r = runner.invoke(dedupe_app, ["scan", str(tmp_path), "--files", "--json"])
    assert r.exit_code == 0
    data = json.loads(r.stdout)
    assert data["duplicates"] >= 1
    assert data["groups"] >= 1
    assert data["moved"] == 0


def test_scan_no_dupes_is_clean(tmp_path):
    (tmp_path / "only.bin").write_bytes(b"alone")
    r = runner.invoke(dedupe_app, ["scan", str(tmp_path), "--files"])
    assert r.exit_code == 0
    assert "No duplicates" in r.stdout


# ── cross-mode overlap: a byte-identical image must count ONCE, not once per mode ──

def test_cross_mode_overlap_counts_once(tmp_path):
    _img(tmp_path / "a.png")
    shutil.copy(tmp_path / "a.png", tmp_path / "b.png")  # byte-identical image
    r = runner.invoke(dedupe_app, ["scan", str(tmp_path), "--images", "--files", "--json"])
    assert r.exit_code == 0
    data = json.loads(r.stdout)
    # Without overlap resolution this would be 2 (file SHA + image dHash). Must be 1.
    assert data["duplicates"] == 1, data
    assert data["groups"] == 1


# ── restore: full round-trip, collision safety, manifest ──────────────────────

def test_manifest_written_on_move(tmp_path):
    _two_dupes(tmp_path)
    qdir = tmp_path / "_q"
    runner.invoke(dedupe_app, ["scan", str(tmp_path), "--files", "--move", str(qdir)])
    m = qdir / _MANIFEST
    assert m.exists()
    data = json.loads(m.read_text("utf-8"))
    assert data["version"] == 1
    assert len(data["moved"]) == 1
    assert {"from", "to"} <= set(data["moved"][0])


def test_restore_round_trip(tmp_path):
    _two_dupes(tmp_path)
    qdir = tmp_path / "_q"
    runner.invoke(dedupe_app, ["scan", str(tmp_path), "--files", "--move", str(qdir)])
    rr = runner.invoke(dedupe_app, ["restore", str(qdir)])
    assert rr.exit_code == 0, rr.stdout
    assert "Restored 1" in rr.stdout
    # both originals back, and the manifest is cleared (nothing left to restore)
    assert (tmp_path / "x.bin").exists() and (tmp_path / "y.bin").exists()
    assert not (qdir / _MANIFEST).exists()


def test_restore_collision_left_in_quarantine(tmp_path):
    _two_dupes(tmp_path)
    qdir = tmp_path / "_q"
    runner.invoke(dedupe_app, ["scan", str(tmp_path), "--files", "--move", str(qdir)])
    moved_from = json.loads((qdir / _MANIFEST).read_text("utf-8"))["moved"][0]["from"]
    (tmp_path / moved_from).write_bytes(b"same-bytes")  # re-occupy the original path
    rr = runner.invoke(dedupe_app, ["restore", str(qdir)])
    assert rr.exit_code == 0
    assert "left in quarantine" in rr.stdout
    assert (qdir / _MANIFEST).exists()  # manifest kept for the un-restored entry


def test_restore_without_manifest_errors(tmp_path):
    empty = tmp_path / "not_a_quarantine"
    empty.mkdir()
    rr = runner.invoke(dedupe_app, ["restore", str(empty)])
    assert rr.exit_code == 1
    assert "No restore manifest" in rr.stdout


# ── quarantine dir inside the scanned root must not be re-quarantined ──────────

def test_quarantine_inside_root_not_requarantined(tmp_path):
    _two_dupes(tmp_path)
    qdir = tmp_path / "_dupes"  # INSIDE the scanned root
    r1 = runner.invoke(dedupe_app, ["scan", str(tmp_path), "--files", "-r", "--move", str(qdir), "--json"])
    assert json.loads(r1.stdout)["moved"] == 1
    # repeat the SAME move op recursively — the already-quarantined file (and manifest)
    # must not be moved again, and the live original must stay put
    r2 = runner.invoke(dedupe_app, ["scan", str(tmp_path), "--files", "-r", "--move", str(qdir), "--json"])
    assert json.loads(r2.stdout)["moved"] == 0
    assert (tmp_path / "x.bin").exists()  # the survivor was never pushed into quarantine


# ── --verbose lists filenames; --fail-on-dupes gates CI ───────────────────────

def test_verbose_lists_duplicate_filenames(tmp_path):
    _two_dupes(tmp_path)
    r = runner.invoke(dedupe_app, ["scan", str(tmp_path), "--files", "-v"])
    assert r.exit_code == 0
    # the compact view only shows counts; -v names the kept file AND the duplicate
    assert "keep" in r.stdout
    assert "x.bin" in r.stdout and "y.bin" in r.stdout


def test_fail_on_dupes_exits_nonzero(tmp_path):
    _two_dupes(tmp_path)
    r = runner.invoke(dedupe_app, ["scan", str(tmp_path), "--files", "--fail-on-dupes"])
    assert r.exit_code == 1


def test_fail_on_dupes_clean_is_zero(tmp_path):
    (tmp_path / "only.bin").write_bytes(b"solo")
    r = runner.invoke(dedupe_app, ["scan", str(tmp_path), "--files", "--fail-on-dupes"])
    assert r.exit_code == 0


def test_fail_on_dupes_still_emits_json(tmp_path):
    _two_dupes(tmp_path)
    r = runner.invoke(dedupe_app, ["scan", str(tmp_path), "--files", "--fail-on-dupes", "--json"])
    assert r.exit_code == 1
    assert json.loads(r.stdout)["duplicates"] >= 1  # JSON printed despite non-zero exit
