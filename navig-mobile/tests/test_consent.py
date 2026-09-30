"""Consent gate + evidence case-dir (chain-of-custody) tests."""

from __future__ import annotations

import pytest

from navig_mobile.consent import (
    CaseDir,
    ConsentGate,
    ConsentRefused,
    ConsentRequired,
    _hash_path,
)
from navig_mobile.store import get_store


def _gate(tmp_path):
    store = get_store(db_path=tmp_path / "c.db")
    return ConsentGate(store=store), store


def test_consent_record_and_require(tmp_path):
    gate, _ = _gate(tmp_path)
    with pytest.raises(ConsentRequired):
        gate.require("A1", "spyware scan")
    gate.record(udid="A1", authorization_ref="I own this device", scope="all", operator="me")
    rec = gate.require("A1", "spyware scan")
    assert rec["authorization_ref"] == "I own this device"
    assert rec["scope"] == "all"


def test_consent_refuses_empty_authorization(tmp_path):
    gate, _ = _gate(tmp_path)
    with pytest.raises(ConsentRefused):
        gate.record(udid="A1", authorization_ref="   ")


def test_consent_expiry_blocks(tmp_path):
    gate, store = _gate(tmp_path)
    store.record_consent(udid="A1", authorization_ref="ref",
                         expires_at="2000-01-01T00:00:00.000000Z")
    assert gate.check("A1") is None
    with pytest.raises(ConsentRequired):
        gate.require("A1", "x")


def test_consent_revoke(tmp_path):
    gate, _ = _gate(tmp_path)
    gate.record(udid="A1", authorization_ref="ref")
    assert gate.check("A1") is not None
    assert gate.revoke("A1") == 1
    assert gate.check("A1") is None


def test_normalize_until():
    assert ConsentGate.normalize_until("2026-12-31") == "2026-12-31T23:59:59.999999Z"
    assert ConsentGate.normalize_until(None) is None
    assert ConsentGate.normalize_until("2026-12-31T10:00:00Z") == "2026-12-31T10:00:00Z"


def test_normalize_until_rejects_garbage():
    # M4: unparseable expiry must raise (not silently store a value that
    # string-compares to never-expires / instantly-expired).
    for bad in ("soon", "2026-13-99", "next week", "12/31/2026"):
        with pytest.raises(ValueError):
            ConsentGate.normalize_until(bad)


def test_consent_record_rejects_bad_until(tmp_path):
    gate, _ = _gate(tmp_path)
    with pytest.raises(ConsentRefused):
        gate.record(udid="A1", authorization_ref="I own this device", until="notadate")


# ── CaseDir / chain of custody ───────────────────────────────────────────────

def test_casedir_evidence_and_manifest(tmp_path):
    cd = CaseDir.create(udid="A1", platform="android", name="t",
                        authorization_ref="I own this device", scope="all", base=tmp_path)
    f = cd.subdir("acquisition") / "x.bin"
    f.write_bytes(b"hello")
    entry = cd.add_evidence(f, tool="test", source="device:A1")
    assert entry["evidence_id"] == "evd_0001"
    assert entry["bytes"] == 5 and entry["sha256"]
    # persisted + reopenable
    reopened = CaseDir.open(cd.root)
    assert reopened.manifest["authorization_ref"] == "I own this device"
    assert len(reopened.manifest["artifacts"]) == 1
    assert reopened.manifest["operator"] is not None


def test_casedir_verify_detects_tamper(tmp_path):
    cd = CaseDir.create(udid="A1", platform="android", base=tmp_path)
    f = cd.subdir("acquisition") / "x.bin"
    f.write_bytes(b"original")
    cd.add_evidence(f, tool="t")
    assert cd.verify()[0]["ok"] is True
    f.write_bytes(b"TAMPERED-LONGER")  # mutate the original
    v = cd.verify()[0]
    assert v["ok"] is False and v["missing"] is False


def test_casedir_verify_detects_missing(tmp_path):
    cd = CaseDir.create(udid="A1", platform="android", base=tmp_path)
    f = cd.subdir("acquisition") / "x.bin"
    f.write_bytes(b"data")
    cd.add_evidence(f, tool="t")
    f.unlink()
    v = cd.verify()[0]
    assert v["ok"] is False and v["missing"] is True


def test_hash_path_dir_is_stable(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    (d / "a").write_bytes(b"a")
    (d / "b").write_bytes(b"bb")
    h1, s1 = _hash_path(d)
    h2, s2 = _hash_path(d)
    assert h1 == h2 and s1 == 3 and h1
