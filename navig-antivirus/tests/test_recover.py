"""Chrome/Edge/Brave profile-index recovery — the ONE write path in the plugin.

Two ways it could hurt the user it's meant to help, both covered here:
  * a torn `Local State` write corrupts the profile index worse than the collapse;
  * the open-browser guard checked the wrong process for Edge/Brave, so a write
    could land under a live browser and be overwritten (or corrupted) on flush.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from navig_antivirus.engine import browsers


def _make_ud(tmp_path: Path, cached=("Default",),
             on_disk=("Default", "Profile 1")) -> Path:
    ud = tmp_path / "User Data"
    ud.mkdir()
    for name in on_disk:
        d = ud / name
        d.mkdir()
        (d / "Preferences").write_text(
            json.dumps({"profile": {"name": name}}), encoding="utf-8"
        )
    info_cache = {n: {"name": n, "metrics_bucket_index": i} for i, n in enumerate(cached)}
    (ud / "Local State").write_text(
        json.dumps({"profile": {"info_cache": info_cache}}), encoding="utf-8"
    )
    return ud


def test_plan_identifies_missing(tmp_path):
    plan = browsers.plan_recovery(_make_ud(tmp_path))
    assert plan.kept == ["Default"]
    assert [d for d, _, _ in plan.readd] == ["Profile 1"]
    assert (plan.indexed_before, plan.indexed_after) == (1, 2)


def test_dry_run_writes_nothing(tmp_path):
    ud = _make_ud(tmp_path)
    before = (ud / "Local State").read_text(encoding="utf-8")
    plan, backup = browsers.recover_profiles(ud, apply=False, backup_dir=tmp_path / "bak")
    assert backup is None
    assert (ud / "Local State").read_text(encoding="utf-8") == before


def test_apply_rebuilds_and_backs_up(tmp_path, monkeypatch):
    ud = _make_ud(tmp_path)
    monkeypatch.setattr(browsers, "browser_running", lambda b="chrome": False)
    plan, backup = browsers.recover_profiles(
        ud, apply=True, backup_dir=tmp_path / "bak", browser="chrome"
    )
    ls = json.loads((ud / "Local State").read_text(encoding="utf-8"))
    assert set(ls["profile"]["info_cache"]) == {"Default", "Profile 1"}
    assert backup and backup.exists()
    assert not (ud / "Local State.navig-tmp").exists()  # no leftover temp


def test_guard_checks_the_selected_browser(tmp_path, monkeypatch):
    ud = _make_ud(tmp_path)
    seen = {}

    def fake_running(b="chrome"):
        seen["browser"] = b
        return True  # pretend it's open

    monkeypatch.setattr(browsers, "browser_running", fake_running)
    with pytest.raises(RuntimeError, match="Edge is running"):
        browsers.recover_profiles(ud, apply=True, backup_dir=tmp_path / "bak", browser="edge")
    assert seen["browser"] == "edge"  # NOT hardcoded to chrome


def test_torn_write_leaves_original_intact(tmp_path, monkeypatch):
    ud = _make_ud(tmp_path)
    monkeypatch.setattr(browsers, "browser_running", lambda b="chrome": False)
    original = (ud / "Local State").read_text(encoding="utf-8")

    def boom(*a, **k):
        raise OSError("simulated crash mid-rename")

    monkeypatch.setattr(browsers.os, "replace", boom)
    with pytest.raises(OSError):
        browsers.recover_profiles(ud, apply=True, backup_dir=tmp_path / "bak")
    # original is byte-for-byte intact; the half-written temp was cleaned up
    assert (ud / "Local State").read_text(encoding="utf-8") == original
    assert not (ud / "Local State.navig-tmp").exists()
