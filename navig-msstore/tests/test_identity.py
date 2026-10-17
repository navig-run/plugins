"""`configure` must never wipe a healthy store/identity.json on a transient read lock,
and its write must be atomic. (msstore's first tests.)"""
from __future__ import annotations

import json

import pytest

from navig_msstore import _IdentityUnreadable, _read_identity_for_update, _write_identity

_TMP = ".navig-tmp"


def _idf(root):
    return root / "store" / "identity.json"


def _seed(root, data):
    idf = _idf(root)
    idf.parent.mkdir(parents=True, exist_ok=True)
    idf.write_text(json.dumps(data), encoding="utf-8")
    return idf


# ── read-for-update: missing/corrupt → {}, but an existing-unreadable file RAISES ──

def test_missing_reads_empty(tmp_path):
    assert _read_identity_for_update(tmp_path) == {}


def test_corrupt_reads_empty(tmp_path):
    _idf(tmp_path).parent.mkdir(parents=True)
    _idf(tmp_path).write_text("{ not json", encoding="utf-8")
    assert _read_identity_for_update(tmp_path) == {}  # unrecoverable → overwritable


def test_existing_but_locked_raises_and_leaves_file_intact(tmp_path, monkeypatch):
    idf = _seed(tmp_path, {"storeProductId": "9PABC", "publisherCN": "CN=Acme"})
    real = type(idf).read_text

    def _locked(self, *a, **k):
        if self == idf:
            raise PermissionError("The process cannot access the file (sharing violation)")
        return real(self, *a, **k)

    monkeypatch.setattr(type(idf), "read_text", _locked)
    with pytest.raises(_IdentityUnreadable):
        _read_identity_for_update(tmp_path)

    # the healthy file is untouched — the guard aborted before any write could wipe it
    monkeypatch.setattr(type(idf), "read_text", real)
    assert json.loads(idf.read_text(encoding="utf-8"))["storeProductId"] == "9PABC"


def test_non_dict_reads_empty(tmp_path):
    _idf(tmp_path).parent.mkdir(parents=True)
    _idf(tmp_path).write_text("[1, 2, 3]", encoding="utf-8")
    assert _read_identity_for_update(tmp_path) == {}


# ── atomic write ──────────────────────────────────────────────────────────────

def test_write_roundtrips_and_leaves_no_temp(tmp_path):
    idf = _idf(tmp_path)
    _write_identity(idf, {"storeProductId": "9PXYZ", "version": "1.2.3"})
    assert json.loads(idf.read_text(encoding="utf-8")) == {"storeProductId": "9PXYZ", "version": "1.2.3"}
    assert not idf.with_name(idf.name + _TMP).exists()


def test_write_failure_preserves_original_and_no_temp(tmp_path, monkeypatch):
    idf = _seed(tmp_path, {"storeProductId": "OLD"})

    def _boom(self, target):
        raise OSError("disk full")

    monkeypatch.setattr(type(idf), "replace", _boom)
    with pytest.raises(OSError):
        _write_identity(idf, {"storeProductId": "NEW"})

    # a failed replace must leave the original intact and drop no half-written temp
    assert json.loads(idf.read_text(encoding="utf-8"))["storeProductId"] == "OLD"
    assert not idf.with_name(idf.name + _TMP).exists()
