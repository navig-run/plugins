"""Tests for the PC drop-folder pipeline: classification, dedupe, localization, extraction."""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import pytest

from navig_mobile.unpack import localization, pc

SEVEN = shutil.which("7z")


# ── classification ──────────────────────────────────────────────────────────

def test_classify_joins_volume_sets_and_flags_junk(tmp_path):
    (tmp_path / "Game.7z.001").write_bytes(b"a")
    (tmp_path / "Game.7z.002").write_bytes(b"b")
    (tmp_path / "Plain.zip").write_bytes(b"c")
    (tmp_path / "app.apk").write_bytes(b"d")
    (tmp_path / "[PS Vita] Handheld.7z").write_bytes(b"e")
    recs = {r["name"]: r for r in pc.classify(tmp_path)}

    assert recs["Game"]["kind"] == "volume"
    assert recs["Game"]["parts"] == ["Game.7z.001", "Game.7z.002"]
    assert recs["Plain"]["kind"] == "archive"
    assert recs["app"]["kind"] == "skip"
    assert "not a Windows game" in recs["app"]["reason"]
    assert recs["[PS Vita] Handheld"]["kind"] == "skip"


def test_classify_incomplete_volume_set_is_skipped(tmp_path):
    (tmp_path / "Broken.7z.002").write_bytes(b"x")
    rec = next(r for r in pc.classify(tmp_path) if r["name"] == "Broken")
    assert rec["kind"] == "skip"
    assert "no .001" in rec["reason"]


def test_ingest_marks_exact_duplicate_downloads(tmp_path):
    (tmp_path / "Going.Medieval.zip").write_bytes(b"identical-bytes")
    (tmp_path / "Going (1).Medieval.zip").write_bytes(b"identical-bytes")
    manifest = pc.ingest(tmp_path, log=lambda _m: None)
    statuses = {n: g["status"] for n, g in manifest["games"].items()}

    assert statuses["Going Medieval"] == "pending"
    dup = next(g for g in manifest["games"].values() if g["status"] == "duplicate")
    assert dup["duplicate_of"] == "Going Medieval"


def test_ingest_is_idempotent_and_keeps_done_status(tmp_path):
    (tmp_path / "Game.zip").write_bytes(b"payload")
    pc.ingest(tmp_path, log=lambda _m: None)
    manifest = pc.load_manifest(tmp_path)
    manifest["games"]["Game"]["status"] = "done"
    manifest["games"]["Game"]["method"] = "7z"
    pc.save_manifest(tmp_path, manifest)

    again = pc.ingest(tmp_path, log=lambda _m: None)
    g = again["games"]["Game"]
    assert g["status"] == "done"
    assert g["method"] == "7z"


# ── language detection ──────────────────────────────────────────────────────

@pytest.mark.parametrize("rel,expected", [
    ("localization/russian/strings.txt", "ru"),
    ("lang/en/dialogs.txt", "en"),
    ("Data/(rus)/text.txt", "ru"),
    ("P_RUS_+_ENG.txt", "multi"),
    ("It Takes Two/data.txt", "und"),
    ("No Man's Sky/sound.txt", "und"),
    ("Deus Ex/text.txt", "und"),
    ("random/notes.txt", "und"),
    ("text/POLISH.cfg", "pl"),
])
def test_detect_lang(rel, expected):
    assert localization.detect_lang(Path(rel)) == expected


def test_collect_groups_files_by_language(tmp_path):
    game = tmp_path
    (game / "data/localization/russian").mkdir(parents=True)
    (game / "data/localization/russian/a.txt").write_text("привет", encoding="utf-8")
    (game / "data/localization/english").mkdir(parents=True)
    (game / "data/localization/english/b.txt").write_text("hello", encoding="utf-8")
    res = localization.collect(game)

    assert set(res["langs"]) >= {"ru", "en"}
    assert (game / "extract/loc/ru/data/localization/russian/a.txt").exists()
    assert (game / "extract/loc/en/data/localization/english/b.txt").exists()
    assert (game / "extract/loc/_index.json").exists()


# ── generic bucketing ───────────────────────────────────────────────────────

def test_generic_never_carves_huge_or_archive_blobs(tmp_path):
    """Regression: carve_ogg read whole blobs — a 35 GB nested .rar OOM-killed the batch."""
    from navig_mobile.unpack import pipeline

    data = tmp_path / "data"
    data.mkdir()
    big = data / "huge.bin"
    with open(big, "wb") as fh:
        fh.truncate(pipeline.MAX_CARVE + 1)  # sparse: over the carve cap, never read
    (data / "nested.rar").write_bytes(b"Rar!\x1a\x07\x00" + b"\0" * (pipeline.BIG_UNKNOWN + 10))
    candidates: list = []
    orig = pipeline.media.carve_ogg
    pipeline.media.carve_ogg = lambda *_a, **_k: candidates.append("called") or 0
    try:
        pipeline.generic(tmp_path, log=lambda _m: None, roots=[data], skip_video=True)
    finally:
        pipeline.media.carve_ogg = orig
    assert candidates == []  # nothing huge, nothing archive-shaped was ever carved


def test_generic_skip_video_leaves_video_out(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "icon.png").write_bytes(b"\x89PNG")
    (data / "intro.mp4").write_bytes(b"video")
    pipeline_out = tmp_path

    from navig_mobile.unpack import pipeline
    pipeline.generic(pipeline_out, log=lambda _m: None, roots=[data], skip_video=True)

    assert (pipeline_out / "media/images/data/icon.png").exists()
    assert not (pipeline_out / "media/video").exists()


# ── extraction ──────────────────────────────────────────────────────────────

@pytest.mark.skipif(SEVEN is None, reason="7z not on PATH")
def test_extract_one_zip_with_video_excluded(tmp_path):
    src = tmp_path / "src.zip"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("Game/readme.txt", "hi")
        z.writestr("Game/intro.mp4", "video-bytes")
        z.writestr("Game/data.json", "{}")
    rec = {"name": "My Game", "kind": "archive", "parts": ["src.zip"], "size": src.stat().st_size}

    pc.extract_one(rec, tmp_path, skip_video=True, log=lambda _m: None)

    assert rec["status"] == "done"
    data = tmp_path / "_games" / "My Game" / "data"
    assert (data / "readme.txt").exists()  # wrapping folder hoisted
    assert (data / "data.json").exists()
    assert not list(data.rglob("*.mp4"))
    assert (tmp_path / "_games" / "My Game" / "_source.json").exists()


def test_purge_videos_tolerates_read_only_files(tmp_path):
    v = tmp_path / "intro.avi"
    v.write_bytes(b"video")
    v.chmod(0o444)  # archives carry read-only attributes; a bare unlink raises PermissionError
    assert pc._purge_videos(tmp_path) == 1
    assert not v.exists()


@pytest.mark.skipif(SEVEN is None, reason="7z not on PATH")
def test_extract_retries_encrypted_zip_with_site_password(tmp_path):
    import subprocess
    src = tmp_path / "Game-RSLOAD.NET-.zip"
    payload = tmp_path / "inner.txt"
    payload.write_text("payload")
    subprocess.run([SEVEN, "a", "-pRSLOAD.NET", "-y", str(src), str(payload)],
                   capture_output=True, check=True)
    payload.unlink()
    rec = {"name": "Game", "kind": "archive", "parts": [src.name], "size": src.stat().st_size}

    pc.extract_one(rec, tmp_path, skip_video=True, log=lambda _m: None)

    assert rec["status"] == "done"
    assert "password=" in rec["method"]
    assert (tmp_path / "_games" / "Game" / "data" / "inner.txt").exists()


@pytest.mark.skipif(SEVEN is None, reason="7z not on PATH")
def test_run_all_is_resumable(tmp_path):
    src = tmp_path / "Game.zip"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("Game/localization/english/t.txt", "hello")
        z.writestr("Game/localization/russian/t.txt", "привет")
    pc.ingest(tmp_path, log=lambda _m: None)

    first = pc.run_all(tmp_path, log=lambda _m: None)
    assert first["done"] == 1
    game_dir = tmp_path / "_games" / "Game"
    assert (game_dir / "REPORT.md").exists()
    assert (game_dir / "extract/loc/ru/data/localization/russian/t.txt").exists()

    second = pc.run_all(tmp_path, log=lambda _m: None)
    assert sum(second.values()) == 0  # nothing left to do — resume skips done games
