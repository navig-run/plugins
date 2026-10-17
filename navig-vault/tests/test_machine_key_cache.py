"""The machine-fingerprint master key is derived once per (salt, machine), not per read.

Re-running Argon2id (~350 ms) on every secret read made listing AI connections cost
1-2 s per agent turn, because it reads one secret per configured provider. In
machine-fingerprint mode the key is a deterministic function of the salt and the
machine identity, so caching it changes latency, not who can derive it. These tests
pin the boundaries that keep the cache safe: it never outlives a salt or machine
change, and it never holds a passphrase-derived key.
"""

from __future__ import annotations

import pytest

from navig_vault import crypto
from navig_vault.crypto import CryptoEngine


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(crypto, "_MACHINE_KEY_CACHE", {})


@pytest.fixture
def kdf_calls(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    """Count real derivations; a cheap stand-in keeps the test fast and exact."""
    calls: list[bytes] = []

    def _fake_kdf(self, material: bytes, salt: bytes) -> bytes:  # noqa: ARG001
        calls.append(material)
        import hashlib

        return hashlib.sha256(b"kdf|" + salt + b"|" + material).digest()

    monkeypatch.setattr(CryptoEngine, "_kdf", _fake_kdf)
    monkeypatch.setattr(CryptoEngine, "_machine_fingerprint", staticmethod(lambda: b"machine-a"))
    return calls


def test_machine_key_is_derived_once_per_vault(tmp_path, kdf_calls):
    engine = CryptoEngine(tmp_path / "v")
    first = engine.derive_key(None)
    assert engine.derive_key(None) == first
    # A second engine on the same vault (same salt) reuses it too.
    assert CryptoEngine(tmp_path / "v").derive_key(None) == first
    assert len(kdf_calls) == 1


def test_a_different_salt_derives_afresh(tmp_path, kdf_calls):
    a = CryptoEngine(tmp_path / "a").derive_key(None)
    b = CryptoEngine(tmp_path / "b").derive_key(None)
    assert a != b, "two vaults (two salts) must never share a cached key"
    assert len(kdf_calls) == 2


def test_a_changed_machine_identity_derives_afresh(tmp_path, kdf_calls, monkeypatch):
    engine = CryptoEngine(tmp_path / "v")
    before = engine.derive_key(None)
    monkeypatch.setattr(CryptoEngine, "_machine_fingerprint", staticmethod(lambda: b"machine-b"))
    after = engine.derive_key(None)
    assert after != before
    assert kdf_calls == [b"machine-a", b"machine-b"]


def test_passphrase_keys_are_never_cached(tmp_path, kdf_calls):
    engine = CryptoEngine(tmp_path / "v")
    engine.derive_key(b"pass")
    engine.derive_key(b"pass")
    assert kdf_calls == [b"pass", b"pass"], "a passphrase key's lifetime is the unlock session"
    assert crypto._MACHINE_KEY_CACHE == {}


def test_cached_key_still_decrypts_what_it_sealed(tmp_path):
    """End to end with the real KDF: caching must not change the key itself."""
    engine = CryptoEngine(tmp_path / "v")
    dek = CryptoEngine.generate_dek()
    sealed = CryptoEngine.seal(engine.derive_key(None), dek)  # derives (and caches)
    crypto._MACHINE_KEY_CACHE.clear()
    fresh = engine.derive_key(None)  # a genuine re-derivation
    assert CryptoEngine.open(fresh, sealed) == dek
    assert CryptoEngine.open(engine.derive_key(None), sealed) == dek  # cached path
