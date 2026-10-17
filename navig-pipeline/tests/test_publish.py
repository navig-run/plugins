"""Posting packs — the right scene's copy, the operator's file left byte-identical."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from navig_pipeline import publish as P

SCENE = {"segments": [
    {"type": "hook", "title": "My AI agent tried to *rm -rf prod*", "sub": "Two guards. **Zero damage.**"},
    {"type": "outro", "cta": "pipx install navig"},
]}


def test_copy_is_drafted_from_the_hook_on_separate_lines() -> None:
    title, caption, tags = P.copy_for(SCENE, None, Path("clip.mp4"))
    assert title == "My AI agent tried to rm -rf prod"
    # joined with a space this read "Two guards. Zero damage. pipx install navig"
    assert caption.splitlines() == ["Two guards. Zero damage.", "→ pipx install navig"]
    assert tags == list(P.DEFAULT_TAGS)


def test_reviewed_posting_copy_wins_and_tags_get_their_hash() -> None:
    title, caption, tags = P.copy_for(SCENE, {"title": "T", "caption": "C", "hashtags": ["devops", "#ai"]},
                                      Path("x.mp4"))
    assert (title, caption, tags) == ("T", "C", ["#devops", "#ai"])


def test_the_render_names_its_own_scene(tmp_path: Path) -> None:
    ep = tmp_path / "e07"
    (ep / "out-v2").mkdir(parents=True)
    (ep / "scene.json").write_text("{}", encoding="utf-8")
    (ep / "scene-v2.json").write_text("{}", encoding="utf-8")
    (ep / "clip.md").write_text('[shot: url="stage:scene.json#0"]', encoding="utf-8")
    (ep / "clip-v2.md").write_text('[shot: url="stage:scene-v2.json#0"]', encoding="utf-8")
    # sorting by name picked scene.json for this render — the old hook on the new video
    assert P.scene_for_clip(ep / "out-v2" / "c.mp4") == ep / "scene-v2.json"
    (ep / "out").mkdir()
    assert P.scene_for_clip(ep / "out" / "c.mp4") == ep / "scene.json"


@pytest.mark.parametrize("eol", ["\n", "\r\n"])
def test_upsert_keeps_the_index_byte_identical_and_its_line_endings(tmp_path: Path, eol: str) -> None:
    index = tmp_path / "POSTING.md"
    original = eol.join(["# Posting pack", "", "| a | b |", ""]).encode("utf-8")
    index.write_bytes(original)
    P.upsert(index, "e07.mp4", "### one\n- x\n")
    P.upsert(index, "e07.mp4", "### two\n- y\n")   # replaces, never duplicates
    data = index.read_bytes()
    assert data.startswith(original.rstrip(eol.encode()))
    assert data.count(b"<!-- publish:e07.mp4 -->") == 1 and b"### two" in data and b"### one" not in data
    if eol == "\n":
        assert b"\r" not in data   # the first real run turned an LF file into CRLF throughout
    else:
        assert data.count(b"\n") == data.count(b"\r\n")


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_build_pack_writes_cover_and_entry_and_reports_specs(tmp_path: Path) -> None:
    clip = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=navy:s=1080x1920:r=30:d=2",
                    "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-shortest",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(clip)],
                   check=True, capture_output=True)
    pack = P.build_pack(clip, scene=SCENE)
    assert pack.cover and pack.cover.is_file() and pack.entry and pack.entry.is_file()
    assert pack.findings == [] and not pack.blocking
    body = pack.entry.read_text(encoding="utf-8")
    assert "  > Two guards. Zero damage." in body and "TikTok by hand" in body


def test_same_named_renders_in_different_folders_get_separate_entries(tmp_path: Path) -> None:
    # the first French publish (out-v2-fr/clip-e07….mp4) replaced the English entry
    # (out-v2/clip-e07….mp4) because blocks were keyed by filename alone
    index = tmp_path / "POSTING.md"
    en = tmp_path / "e07" / "out-v2" / "clip-e07.mp4"
    fr = tmp_path / "e07" / "out-v2-fr" / "clip-e07.mp4"
    assert P.index_key(en, index) == "e07/out-v2/clip-e07.mp4"
    P.upsert(index, P.index_key(en, index), "### English\n")
    P.upsert(index, P.index_key(fr, index), "### Français\n")
    text = index.read_text(encoding="utf-8")
    assert "### English" in text and "### Français" in text and text.count("<!-- publish:") == 2
