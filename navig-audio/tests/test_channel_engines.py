"""Ambience determinism, bed assignment and the cut grid — the channel engines.

The invariants: ambience bytes depend only on (kind, seconds); a bed assignment
is stable and unique per episode and lives in the workspace; the grid cache is
shared with the workspace scripts and a seeded cache is served without
touching ffmpeg.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from navig_audio.ambience import KINDS, amb_wav
from navig_audio.bedgrid import grid
from navig_audio.beds import BedError, assign, mood_for, pool, store_path
from navig_audio.speech import groups, narration

FFMPEG = shutil.which("ffmpeg")


# ── ambience ────────────────────────────────────────────────────────────────
def test_ambience_is_deterministic(tmp_path: Path):
    a = amb_wav("rain", 1.5, tmp_path / "a")
    b = amb_wav("rain", 1.5, tmp_path / "b")
    assert a.read_bytes() == b.read_bytes(), "same kind+length must give same bytes"
    assert a.name == "amb-rain-2.wav"  # length rounds to whole seconds in the name
    # every kind produces a file, and a cache hit reuses it
    for k in KINDS:
        p = amb_wav(k, 1.2, tmp_path)
        assert p.exists()
        m = p.stat().st_mtime
        assert amb_wav(k, 1.2, tmp_path).stat().st_mtime == m  # not regenerated


def test_ambience_rejects_unknown_kind(tmp_path: Path):
    with pytest.raises(ValueError, match="unknown ambience"):
        amb_wav("thunder", 1.0, tmp_path)


# ── beds ────────────────────────────────────────────────────────────────────
@pytest.fixture()
def ws_with_library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace with a two-track 'dark' library (durations stubbed)."""
    ws = tmp_path / "ws"
    (ws / "sound").mkdir(parents=True)
    lib = tmp_path / "lib" / "Dark & Horror"
    lib.mkdir(parents=True)
    for name in ("alpha-horror.mp3", "beta-horror.mp3"):
        (lib / name).write_bytes(b"x")
    monkeypatch.setattr("navig_audio.beds.library_root", lambda ws: tmp_path / "lib")
    monkeypatch.setattr("navig_audio.beds._dur", lambda p: 100.0)
    return ws


def test_bed_assign_is_stable_and_unique(ws_with_library: Path):
    t1, at1 = assign("ep1", "dark", ws_with_library)
    t2, at2 = assign("ep1", "dark", ws_with_library)
    assert (t1, at1) == (t2, at2), "an episode must keep its track across renders"
    t3, _ = assign("ep2", "dark", ws_with_library)
    assert t3 != t1, "no two episodes may share a track"
    store = json.loads(store_path(ws_with_library).read_text(encoding="utf-8"))
    assert set(store) == {"ep1", "ep2"}
    assert store["ep1"]["mood"] == "dark"


def test_bed_assign_exhausts_the_pool_loudly(ws_with_library: Path):
    assign("ep1", "dark", ws_with_library)
    assign("ep2", "dark", ws_with_library)
    with pytest.raises(BedError, match="no free track"):
        assign("ep3", "dark", ws_with_library)


def test_mood_resolution():
    assert mood_for("x", {"mood": "epic"}) == "epic"
    assert mood_for("neighbour", {}) == "dark"        # the channel's own table
    assert mood_for("unknown-episode", {}) == "dark"  # safe default


def test_pool_filters_shortmixes_and_junk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    lib = tmp_path / "lib" / "Dark & Horror"
    lib.mkdir(parents=True)
    for name in ("creeping-horror.mp3", "10 HOUR horror mix.mp3", "unrelated.mp3"):
        (lib / name).write_bytes(b"x")
    monkeypatch.setattr("navig_audio.beds.library_root", lambda ws: tmp_path / "lib")
    names = [p.name for p in pool("dark", tmp_path / "ws")]
    assert names == ["creeping-horror.mp3"]  # the mix is skipped, the unrelated doesn't match


# ── the cut grid ────────────────────────────────────────────────────────────
def test_grid_cache_is_served_without_decoding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A seeded grid must come from the cache — the workspace shares this file."""
    bed = tmp_path / "bed.mp3"
    bed.write_bytes(b"not-audio")  # would fail if ffmpeg were ever invoked
    cache = tmp_path / "grids"
    cache.mkdir()
    import hashlib

    key = hashlib.sha1(f"{bed}|0.0|60.0".encode()).hexdigest()[:12]
    (cache / f"{key}.json").write_text("[0.0, 0.5, 1.0]", encoding="utf-8")
    called = []
    monkeypatch.setattr("navig_audio.bedgrid.subprocess.run", lambda *a, **k: called.append(a))
    beats = grid(bed, 0.0, 60.0, cache)
    assert beats == [0.0, 0.5, 1.0]
    assert not called, "ffmpeg ran despite a seeded cache"


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not on PATH")
def test_grid_measures_a_click_track(tmp_path: Path):
    """A synthetic click track every 0.5 s → a grid near that period, then cached."""
    bed = tmp_path / "clicks.wav"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", "sine=frequency=1000:duration=8", "-af",
         "volume=enable='lt(mod(t,0.5),0.02)':volume=8", str(bed)],
        check=True)
    cache = tmp_path / "grids"
    beats = grid(bed, 0.0, 6.0, cache)
    assert len(beats) >= 8
    assert beats == sorted(beats)
    files = list(cache.glob("*.json"))
    assert len(files) == 1, "one grid per (file, offset, length)"
    # second call: from cache
    again = grid(bed, 0.0, 6.0, cache)
    assert again == beats


# ── speech (the RU/FR/EN contract) ─────────────────────────────────────────
@pytest.mark.parametrize("text,lang,expect", [
    ("1 из 5", "ru", "один из пяти"),
    ("Станция 13010 закрыта в 2021 году.", "ru",
     "Станция тринадцать-ноль-десять закрыта в две тысячи двадцать первом году."),
    ("В 2021 году было 5 из 12.", "ru",
     "В две тысячи двадцать первом году было пять из двенадцати."),
    ("266", "en", "266"),                        # English reads digits fine
    ("2027", "fr", "deux mille vingt-sept"),
])
def test_narration_contract(text, lang, expect):
    assert narration(text, lang) == expect


def test_groups_fold_numbers_onto_display_tokens():
    """'266' on screen = the whole spoken number's timing window."""
    assert groups(["266"], "ru") == [["двести", "шестьдесят", "шесть"]]
    assert groups(["пять"], "en") == [["пять"]]   # untouched when no rule applies
