"""The devhost registry's read-modify-write must never wipe other hosts.

`Registry.load()` used to catch `(json.JSONDecodeError, OSError)` and return an
EMPTY registry — so a transient read lock (a Windows AV/backup sharing violation,
a read landing mid-`os.replace`) during `add`/`remove` (which do
load → mutate → save) would persist an empty registry over every OTHER dev host.
Same "a failed read must never become a destructive write" class the core config
layer guards. The fix routes reads through `navig.core.json_io`: mutators use the
raising `load_for_update()`, read-only views use the degrading `load()`.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def registry_at(tmp_path, monkeypatch):
    """Point the registry at an isolated temp file and return its path."""
    rp = tmp_path / "registry.json"
    monkeypatch.setattr(
        "navig_devhost.engine.registry.registry_path", lambda: rp
    )
    return rp


def _seed_two(monkeypatch):
    from navig_devhost.engine.registry import DevHost, Registry

    reg = Registry()
    reg.put(DevHost(domain="a.test", ip="127.0.0.2", target_port=3000))
    reg.put(DevHost(domain="b.test", ip="127.0.0.3", target_port=3001))
    reg.save()
    return reg


def test_transient_lock_does_not_wipe_registry(registry_at, monkeypatch):
    from navig_devhost.engine.registry import JsonReadError, Registry

    _seed_two(monkeypatch)
    assert {d.domain for d in Registry.load().all()} == {"a.test", "b.test"}

    # Every read attempt hits a sharing violation (AV/backup agent holding the file).
    real_read_text = type(registry_at).read_text

    def _locked(*_a, **_k):
        raise PermissionError("The process cannot access the file (sharing violation)")

    monkeypatch.setattr(type(registry_at), "read_text", _locked)

    # A read-MODIFY-WRITE caller must REFUSE (raise), never silently see an empty
    # registry and then save it over the real one.
    with pytest.raises(JsonReadError):
        Registry.load_for_update()

    # A read-only view degrades to empty (never crashes) — but it writes nothing.
    assert Registry.load().all() == []

    # Release ONLY the read lock (not the registry_path redirect — a bare
    # monkeypatch.undo() would revert to the real store): the file is intact,
    # nothing was wiped.
    monkeypatch.setattr(type(registry_at), "read_text", real_read_text)
    assert {d.domain for d in Registry.load().all()} == {"a.test", "b.test"}


def test_corrupt_registry_is_quarantined_and_recovers(registry_at):
    from navig_devhost.engine.registry import DevHost, Registry

    registry_at.write_text("{ not valid json", encoding="utf-8")

    # Both loaders degrade to empty (the corrupt bytes are already unrecoverable),
    # rather than crashing every `list`/`up`/`add`.
    assert Registry.load().all() == []
    assert Registry.load_for_update().all() == []

    # The bad bytes are preserved beside the store for forensics.
    corrupt = registry_at.parent / (registry_at.name + ".corrupt")
    assert corrupt.read_text(encoding="utf-8") == "{ not valid json"

    # And the store is not wedged — a fresh add succeeds and round-trips.
    reg = Registry.load_for_update()
    reg.put(DevHost(domain="new.test", ip="127.0.0.2", target_port=3000))
    reg.save()
    assert {d.domain for d in Registry.load().all()} == {"new.test"}


def test_wrong_top_level_type_does_not_crash_or_wipe(registry_at):
    """A registry.json that parses to a LIST (not the expected mapping) is treated
    as corruption — quarantined and degraded to empty — not fed to `.get('domains')`
    where it would raise."""
    from navig_devhost.engine.registry import Registry

    registry_at.write_text("[1, 2, 3]", encoding="utf-8")
    assert Registry.load().all() == []
    assert (registry_at.parent / (registry_at.name + ".corrupt")).exists()


def test_missing_registry_is_empty(registry_at):
    from navig_devhost.engine.registry import Registry

    assert not registry_at.exists()
    assert Registry.load().all() == []
    assert Registry.load_for_update().all() == []
