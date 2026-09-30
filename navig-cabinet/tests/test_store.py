"""The cabinet store: any file type round-trips, nothing sensitive is on disk in the clear."""

from __future__ import annotations

import hashlib
import os
import tracemalloc
from pathlib import Path

import pytest
from navig_cabinet import keys
from navig_cabinet.ingest import add_path
from navig_cabinet.store import Cabinet, CabinetError, Duplicate, NotFound, unique_path


def _file(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / "src" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


@pytest.mark.parametrize("name", ["scan.pdf", "id-card.png", "photo.JPG", "voice-note.mp3",
                                  "birth.mp4", "archive.zip", "weird.xyz", "no-extension"])
def test_any_file_type_round_trips_byte_for_byte(cab, tmp_path, name):
    data = os.urandom(3000) + name.encode()
    src = _file(tmp_path, name, data)
    item = add_path(cab, src, read_text=False).item
    out = cab.export(item, tmp_path / "out")
    assert out.read_bytes() == data
    assert out.name == name
    assert src.read_bytes() == data, "the original must never be modified"


def test_kinds_and_default_categories(cab, tmp_path):
    kinds = {n: add_path(cab, _file(tmp_path, n, os.urandom(50)), read_text=False).item
             for n in ["passeport-2031.pdf", "holiday.jpg", "call.m4a", "clip.mov"]}
    assert kinds["passeport-2031.pdf"].kind == "pdf"
    assert kinds["passeport-2031.pdf"].category == "identity"
    assert kinds["holiday.jpg"].category == "photos"
    assert kinds["call.m4a"].kind == "audio" and kinds["call.m4a"].category == "recordings"
    assert kinds["clip.mov"].kind == "video"


def test_nothing_sensitive_is_readable_on_disk(cab, root, tmp_path):
    secret_words = [b"Psychiatric", b"LUMBAR-MRI", b"passport-number-ZZ9", b"sensitive-tag",
                    b"Dr-Secretname"]
    src = _file(tmp_path, "Psychiatric-report.pdf", b"body LUMBAR-MRI body")
    cab.add_stream(src.open("rb"), original_name=src.name, kind="pdf", mime="application/pdf",
                   category="medical", title="Psychiatric report", tags=["sensitive-tag"],
                   issuer="Dr-Secretname", text="passport-number-ZZ9 LUMBAR-MRI")
    cab.close()
    blobs = [p.read_bytes() for p in root.rglob("*") if p.is_file()]
    assert blobs
    for word in secret_words:
        for b in blobs:
            assert word not in b and word.lower() not in b.lower(), word


def test_duplicates_are_refused_unless_allowed(cab, tmp_path):
    src = _file(tmp_path, "a.pdf", b"same bytes")
    first = add_path(cab, src, read_text=False).item
    copy = _file(tmp_path, "copy-of-a.pdf", b"same bytes")
    with pytest.raises(Duplicate) as exc:
        add_path(cab, copy, read_text=False)
    assert exc.value.existing.id == first.id
    assert add_path(cab, copy, read_text=False, allow_duplicate=True).item.id != first.id


def test_a_file_that_changes_mid_add_is_refused(cab, tmp_path):
    src = _file(tmp_path, "a.pdf", b"original")
    with pytest.raises(CabinetError, match="checksum"):
        cab.add_stream(src.open("rb"), original_name="a.pdf", kind="pdf", mime="x",
                       category="other", expected_sha256=hashlib.sha256(b"different").hexdigest())
    assert cab.items() == []
    assert not list((cab.root / "objects").iterdir()), "no orphan object left behind"


def test_tampered_object_is_detected(cab, tmp_path):
    item = add_path(cab, _file(tmp_path, "a.pdf", os.urandom(500)), read_text=False).item
    obj = cab.object_path(item.id)
    raw = bytearray(obj.read_bytes())
    raw[-5] ^= 0xFF
    obj.write_bytes(bytes(raw))
    assert cab.verify(item) is False
    with pytest.raises(Exception):
        cab.export(item, tmp_path / "out")
    assert not list((tmp_path / "out").glob("*")), "a failed export leaves no partial file"


def test_export_never_overwrites(cab, tmp_path):
    a = add_path(cab, _file(tmp_path, "scan.pdf", b"one"), read_text=False).item
    b = add_path(cab, _file(tmp_path / "x", "scan.pdf", b"two"), read_text=False).item
    out = tmp_path / "out"
    out.mkdir()
    (out / "scan.pdf").write_bytes(b"precious")
    pa, pb = cab.export(a, out), cab.export(b, out)
    assert (out / "scan.pdf").read_bytes() == b"precious"
    assert {pa.name, pb.name} == {"scan (2).pdf", "scan (3).pdf"}
    assert unique_path(out, "scan.pdf").name == "scan (4).pdf"


def test_export_strips_path_components_from_names(cab, tmp_path):
    item = cab.add_stream(_file(tmp_path, "x", b"z").open("rb"), original_name="../../evil.txt",
                          kind="text", mime="text/plain", category="other")
    p = cab.export(item, tmp_path / "out")
    assert p.parent == tmp_path / "out" and p.name == "evil.txt"


def test_edit_trash_undelete_purge(cab, tmp_path):
    item = add_path(cab, _file(tmp_path, "a.pdf", b"a"), read_text=False).item
    cab.update(item.id, title="New", tags=["x"], expires="2030-01-02")
    got = cab.get(item.id)
    assert (got.title, got.tags, got.expires) == ("New", ["x"], "2030-01-02")
    cab.set_state(item.id, "trashed")
    assert cab.items() == []
    with pytest.raises(NotFound, match="trash"):
        cab.resolve(item.id)
    cab.set_state(item.id, "active")
    assert cab.resolve(item.id[:4]).id == item.id
    cab.purge(item.id)
    assert not cab.object_path(item.id).exists()
    assert cab.find_by_sha(item.sha256) is None


def test_large_file_streams_in_bounded_memory(cab, tmp_path):
    big = tmp_path / "src" / "video.mp4"
    big.parent.mkdir(parents=True)
    with big.open("wb") as f:
        for _ in range(50):
            f.write(os.urandom(1024 * 1024))
    tracemalloc.start()
    try:
        item = add_path(cab, big, read_text=False).item
        cab.export(item, tmp_path / "out")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 24 * 1024 * 1024, f"peak {peak / 1e6:.1f} MB for a 50 MB file"
    assert (tmp_path / "out" / "video.mp4").stat().st_size == 50 * 1024 * 1024


def test_open_copies_are_swept(cab, tmp_path):
    item = add_path(cab, _file(tmp_path, "a.pdf", b"a"), read_text=False).item
    p = cab.open_copy(item)
    assert p.read_bytes() == b"a"
    assert cab.sweep_open_copies() == 0          # fresh — still being viewed
    old = p.stat().st_mtime - 3600
    os.utime(p, (old, old))
    assert cab.sweep_open_copies() == 1
    assert not p.exists()


# ── keys ────────────────────────────────────────────────────────────────────


def test_machine_mode_reopens(cab, root, tmp_path):
    add_path(cab, _file(tmp_path, "a.pdf", b"a"), read_text=False)
    cab.close()
    assert len(Cabinet.open(root).items()) == 1


def test_passphrase_switch_keeps_every_item_and_wrong_passphrase_fails(cab, root, tmp_path):
    item = add_path(cab, _file(tmp_path, "a.pdf", b"payload"), read_text=False).item
    keys.set_passphrase(root, cab.master_key, "correct horse")
    cab.close()

    with pytest.raises(keys.PassphraseRequired):
        Cabinet.open(root)
    with pytest.raises(keys.WrongKey):
        Cabinet.open(root, "wrong horse")
    with Cabinet.open(root, "correct horse") as c:
        assert c.export(c.get(item.id), tmp_path / "out").read_bytes() == b"payload"
        keys.clear_passphrase(root, c.master_key)
    with Cabinet.open(root) as c:
        assert c.get(item.id).title == "a"


def test_a_passphrase_cabinet_never_falls_back_to_the_machine_key(cab, root):
    keys.set_passphrase(root, cab.master_key, "pw")
    kf = keys.KeyFile.load(root)
    assert kf.mode == "passphrase"
    with pytest.raises(keys.PassphraseRequired):
        kf.unwrap(None)


def test_a_copied_cabinet_does_not_open_on_another_machine(cab, root, monkeypatch):
    monkeypatch.setattr(keys, "machine_material", lambda: (b"navig-cabinet/machine-id/OTHER", "machine-id"))
    with pytest.raises(keys.WrongKey, match="backup"):
        Cabinet.open(root)


def test_kdf_parameters_are_recorded(cab, root):
    kf = keys.KeyFile.load(root)
    assert kf.n == keys.MACHINE_N and kf.machine_source in {"machine-id", "fingerprint"}
    assert keys.KeyFile.load(root).unwrap() == cab.master_key
