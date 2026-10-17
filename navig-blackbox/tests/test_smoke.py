"""Smoke tests for the standalone navig-blackbox engine + _compat fallbacks.

These use isolated temp dirs and never import navig — they prove the package works on its own
(the _compat seam falls back cleanly) and that the .navbox bundle round-trips.
"""
from __future__ import annotations

import sys

import pytest


@pytest.fixture(autouse=True)
def _reset_recorder_singleton():
    import navig_blackbox.recorder as r

    r._recorder = None
    yield
    r._recorder = None


def test_standalone_imports():
    # The public API must import without touching navig.
    from navig_blackbox import (  # noqa: F401
        EventType,
        create_bundle,
        get_recorder,
        install_crash_handler,
        write_bundle,
    )


def test_record_and_read(tmp_path):
    from navig_blackbox.recorder import BlackboxRecorder
    from navig_blackbox.types import EventType

    rec = BlackboxRecorder(tmp_path)
    rec.record(EventType.COMMAND, {"message": "hello"}, source="test")
    rec.record(EventType.ERROR, {"message": "boom"}, source="test")

    assert rec.event_count() == 2
    latest = rec.tail(1)[0]
    assert latest.event_type == EventType.ERROR
    assert latest.payload["message"] == "boom"


def test_bundle_roundtrip(tmp_path):
    from navig_blackbox.bundle import create_bundle, inspect_bundle, write_bundle
    from navig_blackbox.recorder import BlackboxRecorder
    from navig_blackbox.types import EventType

    rec = BlackboxRecorder(tmp_path)
    rec.record(EventType.COMMAND, {"message": "a"})
    rec.record(EventType.WARNING, {"message": "b"})

    b = create_bundle(since_hours=24, blackbox_dir=tmp_path)
    assert b.event_count() == 2

    out = write_bundle(b, tmp_path / "inc.navbox")
    assert out.exists() and out.suffix == ".navbox"

    b2 = inspect_bundle(out)
    assert b2.event_count() == 2
    assert b2.manifest_hash == b.manifest_hash  # round-trip is stable


def test_seal_unseal(tmp_path):
    from navig_blackbox.bundle import create_bundle
    from navig_blackbox.seal import is_sealed, seal_bundle, unseal

    assert not is_sealed(tmp_path)
    seal_bundle(create_bundle(since_hours=0.01, blackbox_dir=tmp_path), blackbox_dir=tmp_path)
    assert is_sealed(tmp_path)
    assert unseal(tmp_path) is True
    assert not is_sealed(tmp_path)


def test_compat_atomic_write_falls_back_without_navig(monkeypatch, tmp_path):
    # Simulate a bare install: navig's yaml_io is unimportable → vendored atomic write is used.
    from navig_blackbox import _compat

    monkeypatch.setitem(sys.modules, "navig.core.yaml_io", None)
    target = tmp_path / "nested" / "marker.txt"
    _compat.atomic_write_text(target, "sealed")
    assert target.read_text(encoding="utf-8") == "sealed"  # parent created, content written


def test_compat_version_is_str():
    from navig_blackbox import _compat

    assert isinstance(_compat.navig_version(), str)


def test_compat_blackbox_dir_honors_navig_data_dir(monkeypatch, tmp_path):
    # navig absent → fallback must honor NAVIG_DATA_DIR (same as navig's own resolution).
    from navig_blackbox import _compat

    monkeypatch.setitem(sys.modules, "navig.platform.paths", None)
    monkeypatch.setenv("NAVIG_DATA_DIR", str(tmp_path))
    assert _compat.blackbox_dir() == tmp_path / "blackbox"


def test_compat_blackbox_dir_has_data_segment(monkeypatch, tmp_path):
    # Regression: the default must be <config>/DATA/blackbox (navig's layout), not <config>/blackbox.
    from navig_blackbox import _compat

    monkeypatch.setitem(sys.modules, "navig.platform.paths", None)
    monkeypatch.delenv("NAVIG_DATA_DIR", raising=False)
    monkeypatch.setenv("NAVIG_CONFIG_DIR", str(tmp_path))
    assert _compat.blackbox_dir() == tmp_path / "data" / "blackbox"
