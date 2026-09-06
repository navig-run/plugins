"""Regression: navig-email's config store must never WIPE rules/briefings.

`update_state()` runs on every unattended `EmailService.tick()`. The old code did
`cfg = load_config()` — which swallowed *any* read error and returned the empty
`_DEFAULT` — then `save_config(cfg)` wrote that empty default back with a non-atomic
`p.write_text`. So a single transient OS lock (AV / backup / half-written read) during a
tick permanently erased every filter rule and briefing schedule.

The store now routes read-modify-writes through `navig.core.json_io`: a transient lock
raises `JsonReadError` and the caller ABORTS the save (no wipe); writes are atomic.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Test THIS checkout's plugin, not whichever copy pip installed editable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from navig.core.json_io import JsonReadError  # noqa: E402
from navig_email import config  # noqa: E402

_POPULATED = {
    "monitor_enabled": True,
    "rules": [{"id": "r1", "name": "Invoices", "channels": ["deck"], "enabled": True}],
    "briefings": [{"id": "b1", "name": "Daily", "cadence": "daily", "channels": ["deck"]}],
    "state": {"seen_ids": ["m1"], "last_brief": {}},
}


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the config store at an isolated temp file, pre-seeded with real records."""
    path = tmp_path / "email" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_POPULATED), encoding="utf-8")
    monkeypatch.setattr(config, "_config_path", lambda: path)
    return path


def _on_disk(path: Path) -> dict:
    # Bypass Path.read_text (which simulate_lock patches) so assertions can read the file.
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def simulate_lock(store, monkeypatch):
    """Simulate a persistent OS lock on the config file at the lowest seam (Path.read_text).
    Both the new store (via json_io's read_text_retrying) AND the old code (via a direct
    p.read_text) bottom out here, so this fixture drives a genuine teeth-check."""
    _orig = Path.read_text

    def _maybe_locked(self, *a, **k):
        if self.name == "config.json":
            raise PermissionError("simulated lock")
        return _orig(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", _maybe_locked)


def test_update_state_preserves_records_on_transient_lock(store, simulate_lock):
    """THE regression: a transient lock during the unattended tick must not wipe records."""
    # Must NOT raise and must NOT touch the file.
    config.update_state(seen_ids=["m1", "m2"])

    saved = _on_disk(store)
    assert saved["rules"] == _POPULATED["rules"], "rules were wiped by a transient lock"
    assert saved["briefings"] == _POPULATED["briefings"], "briefings were wiped"
    assert saved["state"]["seen_ids"] == ["m1"], "file should be untouched after a refused save"


def test_update_state_normal_updates_state_and_keeps_records(store):
    config.update_state(last_brief={"b1": "2026-07-19"})
    saved = _on_disk(store)
    assert saved["rules"] == _POPULATED["rules"]
    assert saved["briefings"] == _POPULATED["briefings"]
    assert saved["state"]["last_brief"] == {"b1": "2026-07-19"}
    assert saved["state"]["seen_ids"] == ["m1"]  # existing state preserved


def test_load_config_degrades_on_unreadable_never_raises(store, simulate_lock):
    """The read-only view must never raise, even on a lock — it just returns defaults."""
    cfg = config.load_config()  # must not raise
    assert cfg["rules"] == []  # degraded to defaults
    assert cfg["monitor_enabled"] is True


def test_load_config_for_update_raises_on_lock(store, simulate_lock):
    """The mutating loader must surface the lock so callers can abort the save."""
    with pytest.raises(JsonReadError):
        config.load_config_for_update()


def test_save_config_roundtrips_and_normalises_ids(store):
    cfg = config.load_config()
    cfg["rules"].append({"name": "No id yet", "channels": ["deck"]})
    config.save_config(cfg)
    saved = _on_disk(store)
    # atomic write round-trips; the id-less rule got an id assigned.
    assert len(saved["rules"]) == 2
    assert all(r.get("id") for r in saved["rules"]), "save_config must assign missing ids"


def test_corrupt_file_does_not_wipe_via_exception(store):
    """A corrupt store is quarantined (data already lost) but update_state must not crash."""
    store.write_text("{ this is not json", encoding="utf-8")
    config.update_state(seen_ids=["x"])  # must not raise
    saved = _on_disk(store)
    assert saved["state"]["seen_ids"] == ["x"]  # rewritten from defaults + the change
    # the corrupt original was quarantined (config.json.corrupt), not silently discarded
    assert (store.parent / (store.name + ".corrupt")).exists()
