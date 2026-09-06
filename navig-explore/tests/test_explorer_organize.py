"""Organize/persist safety for navig-explore — the class-name sanitizer,
collision-safe destinations, and atomic overrides write. Each guards the plugin's
"non-destructive, everything reversible" promise. No HTTP server, no network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from navig_explore import explorer


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("photos", "photos"),
        ("work/2024", "work-2024"),   # path separator → hyphen (stays one segment)
        ("a\\b", "a-b"),              # backslash too
        ("  spaced  ", "spaced"),
        ("", ""),
    ],
)
def test_safe_class_clean(raw, expected):
    assert explorer._safe_class(raw) == expected


@pytest.mark.parametrize("evil", ["../../etc", "..", "/abs/path", "a/../../b", "..\\..\\win"])
def test_safe_class_never_traverses(evil):
    out = explorer._safe_class(evil)
    assert "/" not in out and "\\" not in out and ".." not in out


def test_committed_path_stays_inside_organized():
    """The real security property: no class name can make a commit escape
    ROOT/_ORGANIZED — which would break reversibility (or clobber unrelated files)."""
    root = Path("root").resolve()
    for evil in ["../../../etc", "..\\..\\win", "/abs/path", "a/../../b"]:
        dst = root / "_ORGANIZED" / explorer._safe_class(evil) / "f.jpg"
        assert dst.resolve().is_relative_to(root / "_ORGANIZED")


def test_unique_dst(tmp_path):
    p = tmp_path / "a.jpg"
    assert explorer._unique_dst(p) == p            # free → itself
    p.write_bytes(b"x")
    d1 = explorer._unique_dst(p)
    assert d1 != p and not d1.exists() and d1.name == "a (1).jpg"
    d1.write_bytes(b"y")
    d2 = explorer._unique_dst(p)
    assert d2.name == "a (2).jpg" and not d2.exists()


def test_save_overrides_atomic_roundtrip(tmp_path, monkeypatch):
    ov_path = tmp_path / "overrides.json"
    monkeypatch.setattr(explorer, "OVERRIDES", ov_path)
    data = {"labels": {"a": "photos"}, "deleted": ["b"], "moved": {}}
    explorer._save_overrides(data)
    assert explorer._load_overrides() == data
    assert not (tmp_path / "overrides.json.tmp").exists()  # temp cleaned up
    assert json.loads(ov_path.read_text(encoding="utf-8")) == data  # real file valid


def test_save_overrides_replaces_atomically(tmp_path, monkeypatch):
    ov_path = tmp_path / "overrides.json"
    monkeypatch.setattr(explorer, "OVERRIDES", ov_path)
    explorer._save_overrides({"labels": {"a": "x"}})
    explorer._save_overrides({"labels": {"a": "y"}})
    assert explorer._load_overrides()["labels"]["a"] == "y"


# ── a locked / corrupt READ must never become a destructive WRITE ────────────
# overrides.json is the plugin's entire "non-destructive, everything reversible"
# state. The write was already atomic; these guard the other half — a transiently
# unreadable read (AV/backup lock) or a corrupt file must ABORT the save, not wipe
# every prior label/move/delete with a fresh {}.

def _lock_overrides(monkeypatch, ov_path):
    """Make *ov_path* raise PermissionError on open until unlocked. Trips BOTH the old
    ``json.load(OVERRIDES.open())`` and the new ``read_text_retrying`` path — they both
    bottom out at ``Path.open`` — so the test demonstrates the real wipe on old code."""
    real_open = Path.open
    state = {"locked": True}

    def fake_open(self, *a, **k):
        if state["locked"] and self == ov_path:
            raise PermissionError("file locked by AV/backup")
        return real_open(self, *a, **k)

    monkeypatch.setattr(Path, "open", fake_open)
    return state


def test_save_override_aborts_on_locked_read_no_wipe(tmp_path, monkeypatch):
    ov_path = tmp_path / "overrides.json"
    monkeypatch.setattr(explorer, "OVERRIDES", ov_path)
    original = {"labels": {"a": "photos"}, "deleted": ["b"], "custom_classes": ["photos"]}
    ov_path.write_text(json.dumps(original), encoding="utf-8")
    state = _lock_overrides(monkeypatch, ov_path)
    explorer.save_override("labels", "c", "videos")   # read is locked → must ABORT
    state["locked"] = False                           # unlock so we can read back
    assert json.loads(ov_path.read_text(encoding="utf-8")) == original  # nothing wiped


def test_register_class_aborts_on_locked_read_no_wipe(tmp_path, monkeypatch):
    ov_path = tmp_path / "overrides.json"
    monkeypatch.setattr(explorer, "OVERRIDES", ov_path)
    monkeypatch.setattr(explorer, "KNOWN_CLASSES", set())
    original = {"labels": {"a": "photos"}, "custom_classes": ["photos"]}
    ov_path.write_text(json.dumps(original), encoding="utf-8")
    state = _lock_overrides(monkeypatch, ov_path)
    explorer.register_class("newclass")               # locked → must ABORT
    state["locked"] = False
    assert json.loads(ov_path.read_text(encoding="utf-8")) == original  # nothing wiped
    assert "newclass" not in explorer.KNOWN_CLASSES   # not marked known → retries later


def test_save_override_preserves_prior_entries(tmp_path, monkeypatch):
    ov_path = tmp_path / "overrides.json"
    monkeypatch.setattr(explorer, "OVERRIDES", ov_path)
    ov_path.write_text(json.dumps({"labels": {"a": "photos"}}), encoding="utf-8")
    explorer.save_override("labels", "b", "videos")
    got = json.loads(ov_path.read_text(encoding="utf-8"))
    assert got["labels"] == {"a": "photos", "b": "videos"}  # prior kept + new added


def test_corrupt_overrides_quarantined_not_silently_dropped(tmp_path, monkeypatch):
    ov_path = tmp_path / "overrides.json"
    monkeypatch.setattr(explorer, "OVERRIDES", ov_path)
    ov_path.write_text("{ not valid json", encoding="utf-8")  # corrupt bytes
    explorer.save_override("labels", "a", "photos")           # must not crash
    assert (tmp_path / "overrides.json.corrupt").exists()     # corrupt bytes preserved
    assert json.loads(ov_path.read_text(encoding="utf-8"))["labels"] == {"a": "photos"}
