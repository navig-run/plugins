"""The monitor's state store must not silently reset on a transient/corrupt read.

`load_state` used to return {} whenever the file couldn't be read — a transient lock OR a
truncated write — so main() ran every check against an empty state (losing the edge-trigger
dedup flags → a duplicate alert for everything already alerted), then save_state persisted
that emptiness. Now load_state RAISES StateUnreadable when the file exists but is unreadable,
main() skips the cycle, and save_state writes atomically so a crash can't truncate it.

Pure-stdlib deploy script (json_io isn't importable), so the raise-don't-return-empty
contract is inline. OS-agnostic: the guard is tested directly, not via the Linux checks.
"""

from __future__ import annotations

import pytest

import monitor


def test_save_load_round_trip_and_missing_is_empty(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(monitor, "STATE_FILE", str(state_file))

    assert monitor.load_state() == {}          # missing → fresh (a genuine first run)
    monitor.save_state({"disk_warned": True, "gitea_down": False})
    assert monitor.load_state() == {"disk_warned": True, "gitea_down": False}


def test_load_raises_on_corrupt_file_rather_than_returning_empty(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    state_file.write_text("{ this is not valid json", encoding="utf-8")  # a truncated write
    monkeypatch.setattr(monitor, "STATE_FILE", str(state_file))

    with pytest.raises(monitor.StateUnreadable):
        monitor.load_state()


def test_load_raises_on_locked_file(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    state_file.write_text('{"disk_warned": true}', encoding="utf-8")
    monkeypatch.setattr(monitor, "STATE_FILE", str(state_file))

    import pathlib
    real = pathlib.Path.read_text

    def _locked(self, *a, **k):
        if str(self) == str(state_file):
            raise OSError("sharing violation")   # an AV/backup agent holding the file
        return real(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "read_text", _locked)
    with pytest.raises(monitor.StateUnreadable):
        monitor.load_state()


def test_main_skips_save_when_state_unreadable(monkeypatch):
    """main() must NOT run the checks or save when the state can't be read — otherwise it
    would overwrite the real dedup flags with an empty state."""
    def _raise():
        raise monitor.StateUnreadable("locked")

    saved = {"called": False}
    monkeypatch.setattr(monitor, "load_state", _raise)
    monkeypatch.setattr(monitor, "save_state", lambda *_a, **_k: saved.update(called=True))

    monitor.main()  # must not raise
    assert saved["called"] is False  # the cycle was skipped, no empty-state save
