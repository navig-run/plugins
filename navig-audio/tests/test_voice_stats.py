"""``navig audio voice-stats`` — the cache ledger, read-only.

The cache is two files per line (``<key>.mp3`` + ``<key>.json``); the renderer
needs BOTH, so an orphan is a line that will silently re-bill on its next
render. The stats verb exists to make that visible — counts, size, orphan
names, age — and to never, in any state, mutate the cache it reports on.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from navig_audio.commands.audio import audio_app

runner = CliRunner()


def _seed(cache: Path) -> None:
    """One of every shape: usable pair, orphans of each kind, debris, raw+semi."""
    cache.mkdir(parents=True)
    (cache / "a1.mp3").write_bytes(b"a" * 10)
    (cache / "a1.json").write_text("{}", encoding="utf-8")
    (cache / "d4.mp3").write_bytes(b"d" * 40)
    (cache / "d4.json").write_text("{}", encoding="utf-8")
    (cache / "d4.raw.mp3").write_bytes(b"r" * 50)
    (cache / "d4.semi").write_text("-3", encoding="utf-8")
    (cache / "b2.json").write_text("{}", encoding="utf-8")   # orphan: timings, no audio
    (cache / "c3.mp3").write_bytes(b"c" * 5)                 # orphan: audio, no timings
    (cache / "e5.raw.mp3").write_bytes(b"e" * 7)             # orphan: raw, final gone
    (cache / "f6.tmp.mp3").write_bytes(b"t" * 3)             # crash debris


def test_stats_counts_every_shape_and_names_the_orphans(tmp_path: Path):
    cache = tmp_path / "vo"
    _seed(cache)
    total_bytes = sum(f.stat().st_size for f in cache.iterdir())
    res = runner.invoke(audio_app, ["voice-stats", "--cache-dir", str(cache), "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["exists"] is True
    assert data["mp3"] == 3          # a1, d4, c3
    assert data["json"] == 3         # a1, d4, b2
    assert data["raw"] == 2
    assert data["semi"] == 1
    assert data["tmp"] == 1
    assert data["bytes"] == total_bytes
    assert data["orphans"] == {
        "json_without_mp3": ["b2"],
        "mp3_without_json": ["c3"],
        "raw_without_mp3": ["e5"],
    }
    assert data["oldest"] and data["newest"]
    assert data["dir"] == str(cache)


def test_stats_on_an_empty_cache_is_a_clean_zero(tmp_path: Path):
    cache = tmp_path / "vo"
    cache.mkdir()
    res = runner.invoke(audio_app, ["voice-stats", "--cache-dir", str(cache), "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["exists"] is True
    assert data["mp3"] == data["json"] == data["raw"] == data["semi"] == data["tmp"] == 0
    assert data["bytes"] == 0
    assert data["oldest"] is None and data["newest"] is None
    assert not any(data["orphans"].values())


def test_stats_on_a_never_used_cache_says_so_without_failing(tmp_path: Path):
    gone = tmp_path / "never-created"
    res = runner.invoke(audio_app, ["voice-stats", "--cache-dir", str(gone), "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["exists"] is False
    assert data["mp3"] == 0


def test_stats_human_mode_reports_orphans_and_the_dir(tmp_path: Path):
    cache = tmp_path / "vo"
    _seed(cache)
    res = runner.invoke(audio_app, ["voice-stats", "--cache-dir", str(cache)])
    assert res.exit_code == 0, res.output
    assert "orphan" in res.output
    assert "b2" in res.output and "c3" in res.output
    assert str(cache) in res.output


def test_stats_is_read_only(tmp_path: Path):
    """The ledger must never touch the cache — same bytes before and after."""
    cache = tmp_path / "vo"
    _seed(cache)
    before = sorted((f.name, f.stat().st_size, f.read_bytes()) for f in cache.iterdir())
    res = runner.invoke(audio_app, ["voice-stats", "--cache-dir", str(cache)])
    assert res.exit_code == 0, res.output
    after = sorted((f.name, f.stat().st_size, f.read_bytes()) for f in cache.iterdir())
    assert after == before
