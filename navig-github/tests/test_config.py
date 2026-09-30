"""ConfigManager is navig-github's profile store (a read-modify-write YAML file).

Two ways it could silently destroy saved profiles, both covered here:
  * a transient/corrupt READ came back empty and the next save wiped every sibling;
  * a torn WRITE corrupted profiles.yaml, so the next load read empty → next save wiped.
Plus the one-time adoption of profiles from the pre-0.2.2 default path.
"""

from __future__ import annotations

import pytest

from navig_github.engine import config
from navig_github.engine.config import BackupProfile, ConfigManager


def _prof(name: str) -> BackupProfile:
    return BackupProfile(name=name, target_type="user", target_name="octocat")


def test_roundtrip(tmp_path):
    mgr = ConfigManager(config_dir=tmp_path)
    mgr.save_profile(_prof("a"))
    loaded = mgr.load_profile("a")
    assert loaded is not None and loaded.target_name == "octocat"


def test_save_preserves_siblings(tmp_path):
    mgr = ConfigManager(config_dir=tmp_path)
    mgr.save_profile(_prof("a"))
    mgr.save_profile(_prof("b"))
    assert {p.name for p in mgr.list_profiles()} == {"a", "b"}


def test_corrupt_read_does_not_wipe(tmp_path):
    mgr = ConfigManager(config_dir=tmp_path)
    mgr.save_profile(_prof("a"))
    mgr.save_profile(_prof("b"))
    # a bad read (here: file is a list, not a mapping) must NOT be turned into a
    # destructive write that drops a & b.
    (tmp_path / "profiles.yaml").write_text("- not\n- a\n- mapping\n", encoding="utf-8")
    corrupt = (tmp_path / "profiles.yaml").read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        mgr.save_profile(_prof("c"))
    assert (tmp_path / "profiles.yaml").read_text(encoding="utf-8") == corrupt  # preserved


def test_torn_write_leaves_original_intact(tmp_path, monkeypatch):
    mgr = ConfigManager(config_dir=tmp_path)
    mgr.save_profile(_prof("a"))
    original = (tmp_path / "profiles.yaml").read_text(encoding="utf-8")

    def boom(*a, **k):
        raise OSError("simulated crash mid-rename")

    monkeypatch.setattr(config.os, "replace", boom)
    with pytest.raises(OSError):
        mgr.save_profile(_prof("b"))
    assert (tmp_path / "profiles.yaml").read_text(encoding="utf-8") == original
    assert not (tmp_path / "profiles.yaml.navig-tmp").exists()  # temp cleaned up


def test_delete_missing_returns_false(tmp_path):
    mgr = ConfigManager(config_dir=tmp_path)
    assert mgr.delete_profile("nope") is False  # no raise, no write


def test_legacy_migration_adopts_old_profiles(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "profiles.yaml").write_text(
        "profiles:\n  old:\n    name: old\n    target_type: user\n    target_name: me\n",
        encoding="utf-8",
    )
    new = tmp_path / "new"
    monkeypatch.setattr(ConfigManager, "DEFAULT_CONFIG_DIR", new)
    monkeypatch.setattr(ConfigManager, "LEGACY_CONFIG_DIR", legacy)

    mgr = ConfigManager()  # no arg → default dir → one-time migration runs
    assert mgr.load_profile("old") is not None
    assert (legacy / "profiles.yaml").exists()  # non-destructive: legacy left in place
