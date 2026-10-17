"""Keyslots, recovery, CAS versions, atomic rotation and recipient backups.

Every test runs on a vault in its own tmp dir — never ``get_vault()`` — and
drops the process-wide VMK cache and session around it.
"""

from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timedelta, timezone

import pytest

from navig_vault import keyslots
from navig_vault.backup import BackupError, read_backup, restore_backup, write_backup
from navig_vault.core import Vault, VaultConflict, VaultLocked, acting_as
from navig_vault.crypto import CryptoEngine, CryptoError
from navig_vault.session import SessionStore, VaultSession

SECRET = b'{"value": "s3cr3t-value"}'


@pytest.fixture(autouse=True)
def _clean_process_state():
    keyslots.forget_cached_vmks()
    SessionStore.clear()
    yield
    keyslots.forget_cached_vmks()
    SessionStore.clear()


@pytest.fixture
def vault(tmp_path):
    v = Vault(tmp_path / "vault")
    yield v
    v._store.close()


def _legacy_vault(tmp_path, n: int = 3) -> Vault:
    """A vault as pre-keyslot navig left it: items sealed under KDF(fingerprint)."""
    v = Vault(tmp_path / "legacy")
    v._ensure_initialized = lambda: None  # type: ignore[method-assign]
    for i in range(n):
        v.put(f"item/{i}", f'{{"value": "v{i}"}}'.encode())
    del v._ensure_initialized
    assert v.is_legacy()
    return v


def _kinds(v: Vault) -> list[str]:
    return sorted(s.kind for s in v.keyring.slots())


# ── fresh vaults ────────────────────────────────────────────────────────────


def test_a_fresh_vault_gets_a_random_vmk_and_a_device_slot(vault):
    vault.put("a", SECRET)

    assert not vault.is_legacy()
    expected_device = "dpapi" if sys.platform == "win32" else "keyfile"
    assert _kinds(vault) == sorted([expected_device, "fingerprint"])
    vmk = vault._master_key()
    assert vmk != vault._engine.derive_key(None), "the VMK must not be the fingerprint key"
    assert vault.get_bytes("a") == SECRET


def test_the_device_slot_alone_opens_the_vault(vault):
    vault.put("a", SECRET)
    vault.keyring.remove("fingerprint")
    keyslots.forget_cached_vmks()

    assert _kinds(vault) == ["dpapi" if sys.platform == "win32" else "keyfile"]
    assert vault.get_bytes("a") == SECRET


def test_the_keyfile_protector_round_trips_on_any_platform(vault, monkeypatch, tmp_path):
    monkeypatch.setenv("NAVIG_VAULT_DEVICE_KEY_FILE", str(tmp_path / "dev.key"))
    vmk = b"k" * 32
    slot = vault.keyring.wrap("keyfile", vmk)
    assert vault.keyring._unwrap(slot) == vmk
    (tmp_path / "dev.key").unlink()
    with pytest.raises(CryptoError):
        vault.keyring._unwrap(slot)


def test_the_last_way_in_cannot_be_removed(vault):
    vault.put("a", SECRET)
    vault.keyring.remove("fingerprint")
    with pytest.raises(RuntimeError, match="nothing else would unlock"):
        vault.keyring.remove("dpapi" if sys.platform == "win32" else "keyfile")


# ── legacy migration ────────────────────────────────────────────────────────


def test_migration_keeps_every_item_and_older_navig_can_still_read(tmp_path):
    v = _legacy_vault(tmp_path)
    legacy_key = v._engine.derive_key(None)

    made = v.migrate_to_keyslots()

    assert "fingerprint" in made and not v.is_legacy()
    for i in range(3):
        assert v.get_bytes(f"item/{i}") == f'{{"value": "v{i}"}}'.encode()
    # Downgrade safety: the VMK IS the legacy key, so pre-keyslot code still opens DEKs.
    item = v._store.get("item/0")
    CryptoEngine.open(legacy_key, item.encrypted_dek)


def test_migration_is_idempotent(tmp_path):
    v = _legacy_vault(tmp_path)
    assert v.migrate_to_keyslots()
    slots_before = [s.id for s in v.keyring.slots()]
    assert v.migrate_to_keyslots() == []
    assert [s.id for s in v.keyring.slots()] == slots_before


def test_migration_refuses_a_key_that_does_not_open_the_items(tmp_path, monkeypatch):
    v = _legacy_vault(tmp_path)
    monkeypatch.setattr(CryptoEngine, "_machine_fingerprint", staticmethod(lambda: b"another-machine"))

    with pytest.raises(CryptoError):
        v.migrate_to_keyslots()
    assert v.is_legacy() and v.keyring.slots() == [], "a failed migration must write nothing"


# ── passphrase + session ────────────────────────────────────────────────────


def test_a_passphrase_slot_opens_and_a_wrong_one_does_not(vault):
    vault.unlock(passphrase=b"correct horse")
    vault.put("a", SECRET)
    assert "passphrase" in _kinds(vault)
    vault.lock()

    with pytest.raises(VaultLocked):
        vault.unlock(passphrase=b"wrong")
    vault.unlock(passphrase=b"correct horse")
    assert vault.get_bytes("a") == SECRET


def test_an_expired_legacy_passphrase_session_refuses_instead_of_switching_keys(tmp_path):
    v = _legacy_vault(tmp_path, n=1)
    long_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    SessionStore.set(
        VaultSession(master_key=b"p" * 32, unlocked_at=long_ago, ttl_seconds=60, last_used=long_ago)
    )
    with pytest.raises(VaultLocked, match="expired"):
        v.put("new", SECRET)


# ── recovery ────────────────────────────────────────────────────────────────


def test_recovery_key_is_shown_once_and_verifies(vault):
    vault.put("a", SECRET)
    key = vault.add_recovery()

    assert key.startswith("NAVIG-RK1-")
    assert vault.verify_recovery(key)
    assert vault.verify_recovery(key.lower().replace("-", " "))  # typed back by hand
    assert not vault.verify_recovery(keyslots.format_recovery_key(b"x" * 32))  # another key
    # Nothing on disk holds the private key.
    raw = keyslots.parse_recovery_key(key)
    for path in vault.vault_dir.rglob("*"):
        if path.is_file():
            assert raw not in path.read_bytes(), f"recovery key found in {path.name}"


def test_a_new_recovery_key_replaces_the_old_one(vault):
    vault.put("a", SECRET)
    first = vault.add_recovery()
    second = vault.add_recovery()
    assert vault.verify_recovery(second) and not vault.verify_recovery(first)


# ── versions / CAS ──────────────────────────────────────────────────────────


def test_expected_version_is_compare_and_swap(vault):
    vault.put("a", b"1", expected_version=0)
    with pytest.raises(VaultConflict) as exc:
        vault.put("a", b"x", expected_version=0)
    assert exc.value.current_version == 1
    vault.put("a", b"2", expected_version=1)
    assert vault.get_bytes("a") == b"2" and vault.version_of("a") == 2
    assert [h["version"] for h in vault._store.history("a")] == [1]


def test_concurrent_writers_at_the_same_version_get_exactly_one_conflict(vault):
    vault.put("a", b"0")
    results: list[str] = []
    barrier = threading.Barrier(2)

    def writer(tag: bytes) -> None:
        barrier.wait()
        try:
            vault.put("a", tag, expected_version=1)
            results.append("ok")
        except VaultConflict:
            results.append("conflict")

    threads = [threading.Thread(target=writer, args=(t,)) for t in (b"A", b"B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == ["conflict", "ok"]
    assert vault.version_of("a") == 2


def test_audit_names_the_actor_and_records_deletes(vault):
    vault.put("a", SECRET)
    with acting_as("device:abc"):
        vault.get_bytes("a")
    item_id = vault._store.get("a").id
    vault.delete("a")

    rows = vault._store.get_audit(item_id)
    assert ("accessed", "device:abc") in {(r["action"], r["actor"]) for r in rows}
    assert "delete" in {r["action"] for r in rows}
    assert all("s3cr3t" not in (r["detail"] or "") for r in rows)


# ── rotation ────────────────────────────────────────────────────────────────


def test_rotation_replaces_the_vmk_and_keeps_every_item_and_old_version(vault):
    vault.put("a", b"old")
    vault.put("a", b"new")
    vault.put("b", SECRET)
    key = vault.add_recovery()
    old_vmk = vault._master_key()

    assert vault.rotate() == 2

    new_vmk = vault._master_key()
    assert new_vmk != old_vmk
    assert vault.get_bytes("a") == b"new" and vault.get_bytes("b") == SECRET
    with vault._store._lock:
        hist = vault._store._connect().execute("SELECT encrypted_dek FROM vault_item_history").fetchall()
    for row in hist:
        CryptoEngine.open(new_vmk, row["encrypted_dek"])
    assert vault.verify_recovery(key), "rotation must re-wrap the recovery slot, not drop it"


def test_a_failed_rotation_changes_nothing(vault):
    vault.put("a", SECRET)
    vault.put("b", SECRET)
    vmk_before = vault._master_key()
    with vault._store.transaction() as conn:
        conn.execute("UPDATE vault_items SET encrypted_dek = ? WHERE label = 'b'", (b"\0" * 60,))
    check_before = vault._store.meta_get("vmk_check")

    with pytest.raises(CryptoError):
        vault.rotate()

    keyslots.forget_cached_vmks()
    assert vault._store.meta_get("vmk_check") == check_before
    assert vault._master_key() == vmk_before
    assert vault.get_bytes("a") == SECRET


def test_rotation_requires_the_passphrase_when_a_passphrase_slot_exists(vault):
    vault.unlock(passphrase=b"pw")
    vault.put("a", SECRET)
    with pytest.raises(ValueError, match="passphrase"):
        vault.rotate()


# ── backups ─────────────────────────────────────────────────────────────────


def test_backup_restores_into_a_clean_directory(vault, tmp_path):
    for i in range(4):
        vault.put(f"host/h{i}/ssh_password", f'{{"value": "pw{i}"}}'.encode())
    key = vault.add_recovery()
    out = tmp_path / "b" / "vault.backup.json"

    summary = write_backup(vault, out)
    assert summary["items"] == 4
    assert b"pw0" not in out.read_bytes()

    keyslots.forget_cached_vmks()
    result = restore_backup(out, key, tmp_path / "restored")

    assert result["items"] == 4
    restored = Vault(tmp_path / "restored")
    for i in range(4):
        assert restored.get_bytes(f"host/h{i}/ssh_password") == f'{{"value": "pw{i}"}}'.encode()
    assert restored.verify_recovery(key), "the restored vault keeps the same recovery key"
    restored._store.close()


def test_a_backup_needs_a_recovery_slot(vault, tmp_path):
    vault.put("a", SECRET)
    with pytest.raises(BackupError, match="recovery"):
        write_backup(vault, tmp_path / "x.json")


def test_a_backup_refuses_the_wrong_key_and_any_tampering(vault, tmp_path):
    vault.put("a", SECRET)
    vault.put("b", b'{"value": "other"}')
    key = vault.add_recovery()
    out = tmp_path / "v.json"
    write_backup(vault, out)

    with pytest.raises(BackupError, match="does not belong"):
        read_backup(out, keyslots.format_recovery_key(b"y" * 32))

    body = json.loads(out.read_text(encoding="utf-8"))
    body["items"][0]["label"], body["items"][1]["label"] = body["items"][1]["label"], body["items"][0]["label"]
    out.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(BackupError, match="integrity"):
        read_backup(out, key)


def test_restore_never_overwrites_a_live_vault(vault, tmp_path):
    vault.put("a", SECRET)
    key = vault.add_recovery()
    out = tmp_path / "v.json"
    write_backup(vault, out)
    with pytest.raises(BackupError, match="already holds a vault"):
        restore_backup(out, key, vault.vault_dir)


# ── items sealed under another key (found on a real 96-item vault, 2026-10-16) ──


def _orphan(vault: Vault, label: str) -> None:
    """Re-seal *label*'s DEK under a key this vault does not have."""
    item = vault._store.get(label)
    with vault._store.transaction() as conn:
        conn.execute(
            "UPDATE vault_items SET encrypted_dek = ? WHERE id = ?",
            (CryptoEngine.seal(b"z" * 32, b"d" * 32), item.id),
        )


def test_a_backup_leaves_out_and_names_items_nothing_can_open(vault, tmp_path):
    vault.put("good", SECRET)
    vault.put("old/orphan", SECRET)
    _orphan(vault, "old/orphan")
    key = vault.add_recovery()
    out = tmp_path / "v.json"

    summary = write_backup(vault, out)

    assert summary["items"] == 1 and summary["skipped_unreadable"] == ["old/orphan"]
    keyslots.forget_cached_vmks()
    assert restore_backup(out, key, tmp_path / "r")["items"] == 1, "the backup must stay restorable"


def test_rotation_refuses_by_name_when_an_item_cannot_be_opened(vault):
    vault.put("good", SECRET)
    vault.put("old/orphan", SECRET)
    _orphan(vault, "old/orphan")
    assert vault.unreadable_items() == ["old/orphan"]
    with pytest.raises(CryptoError, match="old/orphan"):
        vault.rotate()
    assert vault.get_bytes("good") == SECRET