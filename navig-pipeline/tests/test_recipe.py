"""Episode recipes — byte-exact round trips, and a recipe never chooses where it lands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from navig_pipeline import recipe as R


@pytest.fixture()
def episode(tmp_path: Path) -> Path:
    ep = tmp_path / "e07"
    (ep / "_vo").mkdir(parents=True)
    scene = {"segments": [{"type": "hook", "title": "t", "art": "kraken"},
                          {"type": "terminal", "cast": "1-lock.cast"},
                          {"type": "terminal", "cast": "../e06/check.cast"},
                          {"type": "image", "image": "wifi.svg"}]}
    (ep / "scene-v2.json").write_text(json.dumps(scene), encoding="utf-8")
    # CRLF on purpose: a recipe round-trips bytes, not lines
    with open(ep / "clip-v2.md", "w", encoding="utf-8", newline="") as fh:
        fh.write('---\r\naudio: _vo/track-final.mp3\r\n---\r\n[shot: url="stage:scene-v2.json#0"]\r\n')
    (ep / "_vo" / "build_vo.py").write_text('LINES = [("n1", "daniel", "Hi.", 0.5)]\n', encoding="utf-8")
    (ep / "1-lock.cast").write_text('{"version": 2, "width": 44}\n[0.1, "o", "ok"]\n', encoding="utf-8")
    (ep / "wifi.svg").write_text("<svg/>", encoding="utf-8")
    return ep


def test_round_trip_is_byte_exact_and_idempotent(episode: Path, tmp_path: Path) -> None:
    doc = R.export(episode)
    assert {"scene-v2.json", "clip-v2.md", "_vo/build_vo.py", "1-lock.cast"} <= set(doc["files"])
    target = tmp_path / "restored"
    report = R.apply(doc, target)
    assert not report["conflict"] and len(report["written"]) == len(doc["files"])
    for rel in doc["files"]:
        assert (target / rel).read_bytes() == (episode / rel).read_bytes(), rel
    again = R.apply(doc, target)
    assert again["written"] == [] and len(again["unchanged"]) == len(doc["files"])


def test_assets_are_listed_not_carried_and_brand_names_are_not_files(episode: Path) -> None:
    doc = R.export(episode)
    assert "wifi.svg" in doc["assets"] and "wifi.svg" not in doc["files"]
    assert "kraken" not in doc["assets"]                     # built-in art, not a missing file
    assert doc["assets"]["../e06/check.cast"]["external"] is True
    assert "_vo/track-final.mp3" in doc["assets"]            # regenerable, so only listed


def test_an_external_path_is_never_written(episode: Path, tmp_path: Path) -> None:
    doc = R.export(episode)
    doc["files"]["../escape.txt"] = {"text": "x", "sha256": R._sha(b"x")}
    report = R.apply(doc, tmp_path / "t")
    assert report["refused"] == ["../escape.txt"]
    assert not (tmp_path / "escape.txt").exists()


def test_a_conflict_writes_nothing_at_all(episode: Path, tmp_path: Path) -> None:
    doc = R.export(episode)
    target = tmp_path / "t"
    target.mkdir()
    (target / "scene-v2.json").write_text("{}", encoding="utf-8")
    report = R.apply(doc, target)
    assert report["conflict"] == ["scene-v2.json"]
    assert sorted(p.name for p in target.iterdir()) == ["scene-v2.json"]  # never half-applied
    assert (target / "scene-v2.json").read_text(encoding="utf-8") == "{}"
    assert R.apply(doc, target, force=True)["written"]


def test_a_damaged_or_future_recipe_is_refused(episode: Path, tmp_path: Path) -> None:
    doc = R.export(episode)
    doc["files"]["scene-v2.json"]["text"] += " "
    with pytest.raises(R.RecipeError, match="sha256"):
        R.apply(doc, tmp_path / "t")
    future = tmp_path / "f.recipe.json"
    future.write_text(json.dumps({"format": R.FORMAT, "version": R.VERSION + 1}), encoding="utf-8")
    with pytest.raises(R.RecipeError, match="reads up to"):
        R.load(future)
