"""Sealed SVG exports: the format, its security properties, and vault-to-vault transfer.

Every vault here is ``Vault(tmp_path / …)`` — never the developer's real one (an ad-hoc
probe once wrote a test profile into the real key store). The PBKDF2 cost is lowered;
the format records the count, so nothing else changes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import xml.etree.ElementTree as ET

import pytest

from navig_vault import svg, svg_export, transfer
from navig_vault.core import Vault

PASS = "correct-horse-battery-staple"


@pytest.fixture(autouse=True)
def _fast_kdf(monkeypatch):
    monkeypatch.setattr(svg, "ITERATIONS", 1000)


def _entry(label: str, payload: bytes, **kw) -> dict:
    return {"id": kw.get("id", label + "-id"), "kind": kw.get("kind", "secret"), "label": label,
            "provider": kw.get("provider"), "metadata": {}, "created_at": None, "updated_at": None,
            "last_used_at": None, "version": 1, "payload": base64.b64encode(payload).decode()}


def _info(text: str) -> tuple[str, dict]:
    raw = re.search(r'<info id="svgVaultInfo" encoding="base64">([^<]*)</info>', text).group(1)
    return raw, json.loads(base64.b64decode(raw))


def _with_info(text: str, info: dict) -> str:
    raw, _ = _info(text)
    new = base64.b64encode(json.dumps(info, sort_keys=True, separators=(",", ":")).encode()).decode()
    return text.replace(raw, new)


# ── round trip ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("payload", [b"", b"x", os.urandom(4096), "pässwörd ✓".encode()])
def test_seal_then_open_returns_the_same_entries(payload):
    items = [_entry("one", payload), _entry("two", b'{"api_key": "sk-1"}', kind="provider", provider="openai")]
    text = svg.seal(items, PASS)
    bundle = svg.open_svg(text, PASS)
    assert bundle.items == items
    assert bundle.version == svg.VERSION and bundle.exported_at


def test_the_passphrase_is_normalised_the_same_way_the_opener_does():
    text = svg.seal([_entry("a", b"1")], "  " + PASS + "\n")
    assert svg.open_svg(text, PASS).items[0]["label"] == "a"


def test_an_empty_export_round_trips():
    assert svg.open_svg(svg.seal([], PASS), PASS).items == []


def test_the_output_is_a_well_formed_svg_image():
    root = ET.fromstring(svg.seal([_entry("a", b"1")], PASS))
    assert root.tag == "{http://www.w3.org/2000/svg}svg"
    assert root.find("{http://www.w3.org/2000/svg}rect") is not None  # it draws something


# ── what a stranger holding the file can learn ──────────────────────────────


def test_a_sealed_file_reveals_no_label_provider_or_secret():
    secret = b"ghp_THIS_MUST_NOT_LEAK"
    text = svg.seal([_entry("github-work", secret, provider="github")], PASS)
    for needle in ("github", "work", "ghp_", base64.b64encode(secret).decode()[:12]):
        assert needle not in text
    _, info = _info(text)
    assert set(info) == {"format", "version", "kdf", "cipher", "nonce"}
    found = svg.inspect(text)
    assert found.sealed and found.file_name is None and found.size is None


def test_a_wrong_passphrase_is_refused():
    text = svg.seal([_entry("a", b"1")], PASS)
    with pytest.raises(svg.VaultSvgError, match="wrong passphrase"):
        svg.open_svg(text, PASS + "x")


def test_one_flipped_character_of_the_ciphertext_is_refused():
    text = svg.seal([_entry("a", b"1" * 64)], PASS)
    m = re.search(r'encoding="base64">([^<]{20})', text)
    flipped = m.group(1)[:10] + ("A" if m.group(1)[10] != "A" else "B") + m.group(1)[11:]
    with pytest.raises(svg.VaultSvgError):
        svg.open_svg(text.replace(m.group(1), flipped, 1), PASS)


@pytest.mark.parametrize("edit", [
    lambda i: i["kdf"].update(iterations=i["kdf"]["iterations"] + 1),
    lambda i: i["kdf"].update(salt=base64.b64encode(b"\0" * 16).decode()),
    lambda i: i.update(nonce=base64.b64encode(b"\0" * 12).decode()),
])
def test_editing_the_key_parameters_makes_the_file_refuse_to_open(edit):
    """The info bytes are the AAD: a tampered KDF can't weaken the key, only break the file."""
    text = svg.seal([_entry("a", b"1")], PASS)
    _, info = _info(text)
    edit(info)
    with pytest.raises(svg.VaultSvgError, match="wrong passphrase, or the file was altered"):
        svg.open_svg(_with_info(text, info), PASS)


def test_the_info_block_is_bound_to_the_ciphertext():
    """Only the AAD catches this: an added field changes neither the key nor the nonce.

    (The parametrised test above passes even without the AAD — a changed salt, count or
    nonce already yields a different key — so it cannot prove the binding exists.)
    """
    text = svg.seal([_entry("a", b"1")], PASS)
    _, info = _info(text)
    info["comment"] = "added after sealing"
    with pytest.raises(svg.VaultSvgError, match="wrong passphrase, or the file was altered"):
        svg.open_svg(_with_info(text, info), PASS)


def test_an_absurd_iteration_count_is_refused_before_any_work():
    text = svg.seal([_entry("a", b"1")], PASS)
    _, info = _info(text)
    info["kdf"]["iterations"] = 10**12
    with pytest.raises(svg.VaultSvgError, match="out of range"):
        svg.open_svg(_with_info(text, info), PASS)


def test_entity_declarations_are_refused():
    bomb = ('<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY a "aaaa">]>'
            '<svg xmlns="http://www.w3.org/2000/svg">&a;</svg>')
    with pytest.raises(svg.VaultSvgError, match="entities"):
        svg.inspect(bomb)


# ── passphrases ─────────────────────────────────────────────────────────────


def test_generated_passphrases_carry_over_120_bits_and_use_only_the_alphabet():
    seen = set()
    for _ in range(50):
        p = svg.generate_passphrase()
        groups = p.split("-")
        assert len(groups) == svg.PASSPHRASE_GROUPS
        assert all(len(g) == svg.PASSPHRASE_GROUP_LEN and set(g) <= set(svg.PASSPHRASE_ALPHABET) for g in groups)
        seen.add(p)
        svg.check_passphrase(p)  # always strong enough
    assert len(seen) == 50
    bits = svg.PASSPHRASE_GROUPS * svg.PASSPHRASE_GROUP_LEN * (len(svg.PASSPHRASE_ALPHABET).bit_length() - 1)
    assert bits >= 120


def test_a_short_chosen_passphrase_is_refused():
    with pytest.raises(svg.VaultSvgError, match="at least"):
        svg.check_passphrase("hunter2!")


# ── covers ──────────────────────────────────────────────────────────────────


def test_a_custom_cover_is_kept_byte_for_byte():
    cover = ('<?xml version="1.0"?>\n<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" '
             '"http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">\n'
             '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><circle r="4"/></svg>\n')
    text = svg.seal([_entry("a", b"1")], PASS, cover=cover)
    start, end = text.index('<metadata id="svgVaultMetadata">'), text.index("</metadata>") + len("</metadata>")
    assert text[:start] + text[end:] == cover
    assert svg.open_svg(text, PASS).items[0]["label"] == "a"


def test_a_self_closing_cover_works():
    text = svg.seal([_entry("a", b"1")], PASS, cover='<svg xmlns="http://www.w3.org/2000/svg"/>')
    assert svg.open_svg(text, PASS).items[0]["label"] == "a"


@pytest.mark.parametrize("cover", ["not xml at all", "<html><body/></html>", "<svg"])
def test_a_cover_that_is_not_an_svg_is_refused(cover):
    with pytest.raises(svg.VaultSvgError):
        svg.seal([_entry("a", b"1")], PASS, cover=cover)


def test_a_cover_already_carrying_a_payload_is_refused():
    with pytest.raises(svg.VaultSvgError, match="already carries"):
        svg.seal([_entry("b", b"2")], PASS, cover=svg.seal([_entry("a", b"1")], PASS))


def test_the_generated_picture_carries_no_text():
    assert "<text" not in svg.cover_svg(b"seed") and svg.cover_svg(b"seed") == svg.cover_svg(b"seed")


# ── the original SVG Vault format (1.0, plain) ──────────────────────────────


def _legacy(name: str, data: bytes, *, old_ids: bool = False, extra: dict | None = None) -> str:
    """Exactly what the original web app's xmldom writer emitted."""
    d, i = ("svgArchiveData", "svgArchiveInfo") if old_ids else ("svgVaultData", "svgVaultInfo")
    info = {"fileName": name, "mimeType": "image/png", "size": len(data),
            "encodedAt": "2025-06-13T17:14:57.247Z", "version": "1.0", **(extra or {})}
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="400" height="400" viewBox="0 0 400 400">\n'
            '  <rect width="400" height="400" fill="#9a3c9e"/>\n'
            f'<metadata id="svgVaultMetadata"><data id="{d}" encoding="base64">'
            f'{base64.b64encode(data).decode()}</data><info id="{i}" encoding="base64">'
            f'{base64.b64encode(json.dumps(info).encode()).decode()}</info></metadata></svg>')


@pytest.mark.parametrize("old_ids", [False, True])
def test_old_plain_files_are_still_readable(old_ids):
    data = os.urandom(300)
    text = _legacy("photo.png", data, old_ids=old_ids)
    info = svg.inspect(text)
    assert info.sealed is False and info.file_name == "photo.png" and info.size == 300
    assert svg.open_legacy(text).data == data


def test_an_old_plain_file_with_a_wrong_checksum_is_refused():
    text = _legacy("p.png", b"abc", extra={"sha256": hashlib.sha256(b"other").hexdigest()})
    with pytest.raises(svg.VaultSvgError, match="checksum"):
        svg.open_legacy(text)


def test_an_old_plain_file_is_not_mistaken_for_a_sealed_export():
    with pytest.raises(svg.VaultSvgError, match="unencrypted"):
        svg.open_svg(_legacy("p.png", b"abc"), PASS)


def test_an_svg_without_a_payload_is_reported_as_such():
    assert svg.inspect('<svg xmlns="http://www.w3.org/2000/svg"/>') is None


# ── saving an embedded file never escapes the chosen folder ──────────────────


@pytest.mark.parametrize("name", ["../../evil.txt", "..\\..\\evil.txt", "/etc/evil.txt", "..", ""])
def test_a_hostile_embedded_name_lands_inside_the_folder(tmp_path, name):
    out = tmp_path / "out"
    p = svg_export.save_file(out, name, b"x")
    assert p.parent == out and p.read_bytes() == b"x"


def test_saving_never_overwrites(tmp_path):
    a = svg_export.save_file(tmp_path, "f.txt", b"1")
    b = svg_export.save_file(tmp_path, "f.txt", b"2")
    assert a != b and a.read_bytes() == b"1" and b.read_bytes() == b"2"


# ── vault → SVG → another vault ─────────────────────────────────────────────


@pytest.fixture
def two_vaults(tmp_path):
    a, b = Vault(tmp_path / "a"), Vault(tmp_path / "b")
    a.add("openai", "api_key", {"api_key": "sk-openai-1"})
    a.put("deploy/key", b"\x00\x01binary\xff", kind=transfer.VaultItemKind.FILE)
    a.put("note", "plain text secret".encode())
    return a, b


def test_a_whole_vault_moves_to_another_vault_intact(two_vaults, tmp_path):
    a, b = two_vaults
    report = svg_export.export_svg(a, None, folder=tmp_path / "out", passphrase=PASS)
    [written] = report.files
    assert sorted(written.labels) == ["deploy/key", "note", "openai"]
    bundle = svg.open_svg(written.path.read_text(encoding="utf-8"), PASS)
    res = transfer.import_entries(b, bundle.items)
    assert sorted(res.restored) == ["deploy/key", "note", "openai"] and not res.errors
    for label in res.restored:
        assert b.get_bytes(label) == a.get_bytes(label)
    assert b.get_api_key("openai") == "sk-openai-1"


def test_import_never_overwrites_a_secret_already_here(two_vaults, tmp_path):
    a, b = two_vaults
    b.put("note", b"newer value here")
    entries = transfer.export_entries(a, a.list()).entries
    res = transfer.import_entries(b, entries)
    assert res.skipped == ["note"] and b.get_bytes("note") == b"newer value here"


def test_a_dry_run_writes_nothing(two_vaults):
    a, b = two_vaults
    res = transfer.import_entries(b, transfer.export_entries(a, a.list()).entries, dry_run=True)
    assert len(res.restored) == 3 and b.list() == []


def test_an_id_taken_by_another_label_gets_a_new_one(two_vaults):
    a, b = two_vaults
    [e] = transfer.export_entries(a, [i for i in a.list() if i.label == "note"]).entries
    b.put("something-else", b"x")
    e["id"] = b.list()[0].id
    res = transfer.import_entries(b, [e])
    assert res.restored == ["note"] and b.store().get("note").id != e["id"]


def test_each_writes_one_file_per_item_with_distinct_passphrases_and_no_labels_in_names(two_vaults, tmp_path):
    a, _ = two_vaults
    report = svg_export.export_svg(a, None, folder=tmp_path / "each", each=True)
    assert len(report.files) == 3
    assert len({f.passphrase for f in report.files}) == 3 and all(f.generated for f in report.files)
    for f in report.files:
        assert re.fullmatch(r"vault-[0-9a-f]{8}\.svg", f.path.name)
        assert svg.open_svg(f.path.read_text(encoding="utf-8"), f.passphrase).items[0]["label"] == f.labels[0]


def test_an_unknown_reference_writes_nothing(two_vaults, tmp_path):
    a, _ = two_vaults
    with pytest.raises(svg_export.ExportError, match="matches: nope"):
        svg_export.export_svg(a, ["openai", "nope"], folder=tmp_path / "x")
    assert not (tmp_path / "x").exists()


def test_a_reference_can_be_a_label_an_id_or_a_provider(two_vaults):
    a, _ = two_vaults
    note_id = a.store().get("note").id
    items, missing = transfer.select_items(a, ["openai", note_id, "openai"])
    assert sorted(i.label for i in items) == ["note", "openai"] and not missing


def test_an_existing_output_file_is_never_overwritten(two_vaults, tmp_path):
    a, _ = two_vaults
    out = tmp_path / "x.svg"
    out.write_text("keep me", encoding="utf-8")
    with pytest.raises(svg_export.ExportError, match="already exists"):
        svg_export.export_svg(a, ["note"], folder=tmp_path, out=out, passphrase=PASS)
    assert out.read_text(encoding="utf-8") == "keep me"


def test_the_opener_ships_with_the_package_and_makes_no_network_requests():
    html = svg_export.opener_html()
    assert "default-src 'none'" in html and "crypto.subtle" in html
    assert not re.search(r"""(src|href)=["']https?://""", html)
    assert "innerHTML" not in html  # decrypted values are only ever set as text


def test_an_in_memory_export_writes_nothing_and_opens(two_vaults, tmp_path, monkeypatch):
    """The desktop app receives the SVG text and saves it itself; the daemon writes no file."""
    a, _ = two_vaults
    monkeypatch.chdir(tmp_path)
    before = set(tmp_path.rglob("*"))
    sealed = svg_export.seal_texts(a, ["openai", "note"])
    assert set(tmp_path.rglob("*")) == before
    [f] = sealed.files
    assert re.fullmatch(r"vault-[0-9a-f]{8}\.svg", f.name) and f.generated
    assert sorted(e["label"] for e in svg.open_svg(f.text, f.passphrase).items) == ["note", "openai"]
