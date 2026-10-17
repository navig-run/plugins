"""Doctor unit tests — hermetic (tmp dirs only; never the real OBS config)."""

from __future__ import annotations

import json
from pathlib import Path

from navig_obs import doctor as doc


def test_walk_files_finds_file_settings_with_source_name():
    tree = {
        "sources": [
            {"name": "webcam_full_window", "settings": {"file": "C:/media/webcam.png"}},
            {"name": "Image", "settings": {"file": "https://example.com/x.png"}},  # skipped: URL
            {"name": "ok", "settings": {"file": str(Path(__file__))}},  # exists
        ]
    }
    found = doc._walk_files(tree)
    assert ("webcam_full_window", "C:/media/webcam.png") in found
    assert ("ok", str(Path(__file__))) in found
    assert not any(v.startswith("https://") for _, v in found)


def test_missing_scene_files_reports_only_missing(tmp_path: Path):
    scenes = tmp_path / "basic" / "scenes"
    scenes.mkdir(parents=True)
    missing = str(tmp_path / "gone" / "effect.mp4")
    present = tmp_path / "here.png"
    present.write_bytes(b"x")
    collection = {
        "sources": [
            {"name": "hearts effect", "settings": {"file": missing}},
            {"name": "Image", "settings": {"file": str(present)}},
        ]
    }
    (scenes / "pack.json").write_text(json.dumps(collection), encoding="utf-8")
    (scenes / "pack.json.bak").write_text("{not json", encoding="utf-8")  # must be ignored

    # .bak sidecars are not collections
    assert [c.name for c in doc.scene_collections(tmp_path)] == ["pack.json"]

    report = doc.DoctorReport()
    doc.missing_scene_files(report, config_dir=tmp_path)
    assert len(report.missing_files) == 1
    assert report.missing_files[0]["source"] == "hearts effect"
    assert report.missing_files[0]["file"] == missing


def test_check_plugins_flags_config_without_binary(tmp_path: Path, monkeypatch):
    # Hermetic: point the global plugin roots at tmp so the real machine's
    # ProgramData/AppData DLLs can never leak into this verdict.
    monkeypatch.setenv("ProgramData", str(tmp_path / "programdata"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    install = tmp_path / "obs"
    (install / "obs-plugins" / "64bit").mkdir(parents=True)
    config_dir = tmp_path / "config"
    (config_dir / "plugin_config" / "StreamFX").mkdir(parents=True)
    (config_dir / "plugin_config" / "goodplug").mkdir(parents=True)
    (install / "obs-plugins" / "64bit" / "goodplug.dll").write_bytes(b"MZ")

    report = doc.DoctorReport()
    doc.check_plugins(report, install, config_dir)
    states = {c.name: c for c in report.checks}
    assert states["plugin StreamFX"].state == doc.WARN
    assert "NOT FOUND" in states["plugin StreamFX"].detail
    assert states["plugin goodplug"].state == doc.PASS


def test_binary_present_but_log_says_load_failed_warns(tmp_path: Path, monkeypatch):
    """StreamFX's real failure mode: DLL exists, LoadLibrary still fails."""
    monkeypatch.setenv("ProgramData", str(tmp_path / "programdata"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    install = tmp_path / "obs"
    (install / "obs-plugins" / "64bit").mkdir(parents=True)
    config_dir = tmp_path / "config"
    (config_dir / "plugin_config" / "StreamFX").mkdir(parents=True)
    (install / "obs-plugins" / "64bit" / "StreamFX.dll").write_bytes(b"MZ")
    log_text = (
        "00:16:09.751: LoadLibrary failed for "
        "'C:\\ProgramData\\obs-studio\\plugins\\StreamFX\\bin\\64bit\\StreamFX.dll': "
        "The specified module could not be found.\n"
    )

    report = doc.DoctorReport()
    doc.check_plugins(report, install, config_dir, log_text)
    states = {c.name: c for c in report.checks}
    assert states["plugin StreamFX"].state == doc.WARN
    assert "failed to load" in states["plugin StreamFX"].detail


def test_log_load_failure_without_config_entry_is_reported(tmp_path: Path, monkeypatch):
    """source-copy style: a bare DLL with no plugin_config dir at all."""
    monkeypatch.setenv("ProgramData", str(tmp_path / "programdata"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    config_dir = tmp_path / "config"
    (config_dir / "plugin_config").mkdir(parents=True)
    log_text = (
        "00:16:44.856: Failed to load module file "
        "'../../obs-plugins/64bit/source-copy.dll', module failed to open\n"
    )

    report = doc.DoctorReport()
    doc.check_plugins(report, None, config_dir, log_text)
    states = {c.name: c for c in report.checks}
    assert states["plugin source-copy"].state == doc.WARN
    assert "failed to load" in states["plugin source-copy"].detail.lower()


def test_check_install_reports_on_this_machine():
    # Structural only: whatever the machine, doctor must produce a binary verdict.
    report = doc.DoctorReport()
    install = doc.check_install(report)
    assert len(report.checks) == 1
    verdict = report.checks[0]
    assert verdict.state in (doc.PASS, doc.FAIL)
    if verdict.state == doc.PASS:
        assert install is not None
