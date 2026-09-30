"""Backups restore anywhere (even without navig), paperwork imports, and OCR stays local."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from navig_cabinet import bundle, extract
from navig_cabinet.ingest import add_path, import_paperwork
from navig_cabinet.store import Cabinet


def _file(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / "src" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


@pytest.fixture
def filled(cab, tmp_path):
    items = [
        add_path(cab, _file(tmp_path, "passport.pdf", os.urandom(4000)), read_text=False,
                 expires="2031-05-01", tags=["travel"]).item,
        add_path(cab, _file(tmp_path, "récit médical.jpg", os.urandom(2000)), read_text=False).item,
        add_path(cab, _file(tmp_path, "empty.txt", b""), read_text=False).item,
    ]
    return cab, items


def test_backup_restores_into_a_fresh_cabinet(filled, tmp_path):
    cab, items = filled
    buf = io.BytesIO()
    assert bundle.write_backup(cab, items, buf, "backup pw") == 3

    fresh = Cabinet.create(tmp_path / "elsewhere")
    st = bundle.restore_backup(fresh, io.BytesIO(buf.getvalue()), "backup pw")
    assert (st.restored, st.skipped, st.errors) == (3, 0, [])
    restored = {i.sha256: i for i in fresh.items()}
    for it in items:
        r = restored[it.sha256]
        assert (r.title, r.tags, r.expires, r.original_name) == (it.title, it.tags, it.expires, it.original_name)
        assert fresh.verify(r)
    # restoring twice is free
    again = bundle.restore_backup(fresh, io.BytesIO(buf.getvalue()), "backup pw")
    assert (again.restored, again.skipped) == (0, 3)


def test_backup_wrong_passphrase_and_damage(filled):
    cab, items = filled
    buf = io.BytesIO()
    bundle.write_backup(cab, items, buf, "right")
    with pytest.raises(bundle.BackupError, match="wrong passphrase"):
        bundle.restore_backup(cab, io.BytesIO(buf.getvalue()), "wrong")
    truncated = buf.getvalue()[:-40]
    with pytest.raises(bundle.BackupError):
        bundle.restore_backup(Cabinet.create(cab.root.parent / "t"), io.BytesIO(truncated), "right")


def test_backup_is_not_plaintext(filled):
    cab, items = filled
    buf = io.BytesIO()
    bundle.write_backup(cab, items, buf, "pw")
    assert b"passport" not in buf.getvalue() and b"manifest.json" not in buf.getvalue()


def test_recovery_script_works_without_navig(filled, tmp_path):
    """The escape hatch: stdlib + cryptography turn a backup into an ordinary tar."""
    cab, items = filled
    bak = tmp_path / "b.ncab"
    with bak.open("wb") as f:
        bundle.write_backup(cab, items, f, "pw")
    script = bundle.recovery_script()
    src = script.read_text(encoding="utf-8")
    imports = [ln.split()[1].split(".")[0] for ln in src.splitlines()
               if ln.startswith(("import ", "from "))]
    assert set(imports) <= {"base64", "getpass", "hashlib", "json", "os", "struct", "sys", "cryptography"}

    out = tmp_path / "rec.tar"
    probe = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('rb', {str(script)!r})\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        f"m.recover({str(bak)!r}, {str(out)!r}, 'pw')\n"
        "assert not any(k.startswith('navig') for k in sys.modules), 'navig was imported'\n"
    )
    subprocess.run([sys.executable, "-I", "-c", probe], check=True, timeout=120)
    with tarfile.open(out) as tar:
        names = tar.getnames()
        manifest = json.loads(tar.extractfile("manifest.json").read())
        for entry in manifest["items"]:
            data = tar.extractfile(entry["path"]).read()
            assert hashlib.sha256(data).hexdigest() == entry["sha256"]
    assert "manifest.json" in names and len(manifest["items"]) == 3
    assert any(n.endswith("récit médical.jpg") for n in names)


# ── paperwork handoff ────────────────────────────────────────────────────────


def _manifest(tmp_path, entries):
    p = tmp_path / "handoff.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return p


def _sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_import_paperwork_takes_personal_documents_only(cab, tmp_path):
    cni = _file(tmp_path, "carte-identite.pdf", b"cni")
    ordo = _file(tmp_path, "ordonnance.pdf", b"ordo")
    caf = _file(tmp_path, "caf.pdf", b"caf")
    changed = _file(tmp_path, "changed.pdf", b"now different")
    m = _manifest(tmp_path, [
        {"src": str(cni), "subclass": "identity", "sha256": _sha(cni), "original_name": cni.name},
        {"src": str(ordo), "subclass": "health", "sha256": _sha(ordo), "original_name": ordo.name},
        {"src": str(caf), "subclass": "benefits", "sha256": _sha(caf), "original_name": caf.name},
        {"src": str(changed), "subclass": "identity", "sha256": "0" * 64, "original_name": "changed.pdf"},
        {"src": str(tmp_path / "gone.pdf"), "subclass": "health", "sha256": "1" * 64},
    ])
    report = import_paperwork(cab, m, read_text=False)
    outcomes = {Path(r.src).name: r.outcome for r in report.rows}
    assert outcomes == {"carte-identite.pdf": "imported", "ordonnance.pdf": "imported",
                        "caf.pdf": "skipped", "changed.pdf": "changed", "gone.pdf": "missing"}
    cats = {i.original_name: i.category for i in cab.items()}
    assert cats == {"carte-identite.pdf": "identity", "ordonnance.pdf": "medical"}
    assert all("paperwork" in i.tags for i in cab.items())
    assert cni.exists() and ordo.exists(), "originals are never moved"
    # idempotent
    assert import_paperwork(cab, m, read_text=False).count("already") == 2


# ── extraction stays local and never caches ────────────────────────────────────


def test_extraction_is_local_only_and_uncached(tmp_path, monkeypatch):
    import navig.inbox.extract as ex

    seen = {}

    def spy(path, *, policy=None, budget=None, cache=None):
        seen.update(mode=policy.mode, cache=cache)
        return ex.ExtractResult(text="Blood test results", extracted_by=["pypdf"])

    monkeypatch.setattr(ex, "extract", spy)
    res = extract.extract_text(_file(tmp_path, "a.pdf", b"%PDF"), "pdf")
    assert seen == {"mode": "local", "cache": None}
    assert res.text == "Blood test results" and res.source == "pypdf"


def test_audio_and_video_are_not_transcribed_unless_asked(tmp_path, monkeypatch):
    import navig.inbox.extract as ex

    calls = []
    monkeypatch.setattr(ex, "extract", lambda *a, **k: calls.append(a) or ex.ExtractResult())
    for name, kind in [("a.mp3", "audio"), ("v.mp4", "video")]:
        extract.extract_text(_file(tmp_path, name, b"x"), kind)
    assert calls == []
    extract.extract_text(_file(tmp_path, "b.mp3", b"x"), "audio", transcribe=True)
    assert len(calls) == 1


def test_ocr_text_is_searchable_but_encrypted(cab, root, tmp_path, monkeypatch):
    monkeypatch.setattr(extract, "extract_text",
                        lambda p, k, transcribe=False: extract.TextResult("Hemoglobin 13.2 g/dL", "tesseract"))
    import navig_cabinet.ingest as ingest

    monkeypatch.setattr(ingest, "extract_text", extract.extract_text)
    item = add_path(cab, _file(tmp_path, "labs.png", os.urandom(100))).item
    assert "Hemoglobin" in cab.get(item.id).text
    assert item.category == "medical"  # suggested from the text, not the filename
    cab.close()
    assert not any(b"Hemoglobin" in p.read_bytes() for p in root.rglob("*") if p.is_file())


def test_recovery_script_cli_takes_the_passphrase_from_env(filled, tmp_path):
    """Scripted recovery must not hang on a console prompt (getpass ignores pipes on Windows)."""
    cab, items = filled
    bak = tmp_path / "b.ncab"
    with bak.open("wb") as f:
        bundle.write_backup(cab, items, f, "pw")
    out = tmp_path / "rec.tar"
    env = dict(os.environ, NCAB_PASSPHRASE="pw")
    subprocess.run([sys.executable, "-I", str(bundle.recovery_script()), str(bak), str(out)],
                   check=True, timeout=120, env=env, stdin=subprocess.DEVNULL)
    with tarfile.open(out) as tar:
        assert "manifest.json" in tar.getnames()
