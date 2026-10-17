"""Tests for navig_mobile.unpack — pure-Python extractors exercised on synthetic inputs."""

from __future__ import annotations

import json
import struct
import zipfile

import pytest

from navig_mobile.unpack import kairo, media, pipeline, unity, xnb


# ── Kairosoft archives ──────────────────────────────────────────────────────

def _kairo_archive(files: dict[str, bytes]) -> bytes:
    names = b"".join(struct.pack(">I", len(n.encode())) + n.encode() for n in files)
    count = len(files)
    hdr_len = 12 + len(names) + 8 * count - 4  # data area starts at header_len + 4
    offsets, blobs, off = [], b"", 0
    for data in files.values():
        offsets.append(off)
        chunk = struct.pack(">I", len(data)) + data
        blobs += chunk
        off += len(chunk)
    table = struct.pack(f">{count}I", *offsets) + struct.pack(f">{count}I", *[len(d) for d in files.values()])
    return struct.pack(">III", hdr_len, len(blobs), count) + names + table + blobs


@pytest.mark.parametrize("key", kairo.KNOWN_KEYS)
def test_kairo_round_trip_with_known_keys(tmp_path, key):
    files = {"img.inf": b"0 0 32 32\n", "seb.inf": b"seb", "English.lproj/text.txt": "Morning!\tおはよう".encode()}
    plain = _kairo_archive(files)
    enc = bytes(b ^ key[i % len(key)] for i, b in enumerate(plain))
    src = tmp_path / "src"
    src.mkdir()
    (src / "game.bin").write_bytes(enc)
    result = kairo.unpack_dir(src, tmp_path / "out")
    assert result == {"game.bin": "3 files"}
    for name, data in files.items():
        assert (tmp_path / "out/game" / name).read_bytes() == data


def test_kairo_unknown_cipher_is_reported_not_raised(tmp_path):
    (tmp_path / "x.bin").write_bytes(bytes(range(256)) * 8)
    assert kairo.unpack_dir(tmp_path, tmp_path / "out")["x.bin"].startswith("FAILED")


# ── Ogg carving ─────────────────────────────────────────────────────────────

def _ogg_page(serial: int, seq: int, flags: int, payload: bytes) -> bytes:
    segs = [255] * (len(payload) // 255) + [len(payload) % 255]
    head = b"OggS" + bytes([0, flags]) + struct.pack("<qIII", 0, serial, seq, 0) + bytes([len(segs)]) + bytes(segs)
    page = head + payload
    crc = media._ogg_crc(page)
    return page[:22] + struct.pack("<I", crc) + page[26:]


def test_carve_ogg_recovers_complete_streams_only(tmp_path):
    good = _ogg_page(7, 0, 0x02, b"\x01vorbis" + b"a" * 30) + _ogg_page(7, 1, 0x04, b"b" * 300)
    broken = _ogg_page(9, 0, 0x02, b"\x01vorbis")  # no EOS page → must not be written
    container = tmp_path / "blob.dat"
    container.write_bytes(b"junk" * 100 + good + b"\x00" * 50 + broken + b"tail")
    n = media.carve_ogg(container, tmp_path / "out")
    assert n == 1
    (out,) = list((tmp_path / "out").iterdir())
    assert out.read_bytes() == good


def test_carve_ogg_skips_pages_with_bad_crc(tmp_path):
    page = bytearray(_ogg_page(3, 0, 0x06, b"\x01vorbis"))
    page[-1] ^= 0xFF
    (tmp_path / "c.bin").write_bytes(bytes(page))
    assert media.carve_ogg(tmp_path / "c.bin", tmp_path / "o") == 0


# ── XNB ─────────────────────────────────────────────────────────────────────

def _v7(n: int) -> bytes:
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def _s(text: str) -> bytes:
    raw = text.encode()
    return _v7(len(raw)) + raw


def test_xnb_dictionary_int_string_round_trip(tmp_path):
    readers = [
        "Microsoft.Xna.Framework.Content.DictionaryReader`2[[System.Int32, mscorlib],[System.String, mscorlib]]",
        "Microsoft.Xna.Framework.Content.Int32Reader",
        "Microsoft.Xna.Framework.Content.StringReader",
    ]
    body = _v7(len(readers)) + b"".join(_s(r) + struct.pack("<i", 0) for r in readers) + _v7(0)
    entries = {0: "Weeds/0/-1/Basic", 388: "Wood/2/-300/Basic"}
    body += _v7(1) + struct.pack("<i", len(entries))
    for k, v in entries.items():
        body += struct.pack("<i", k) + _v7(3) + _s(v)  # value: reader index 3 (StringReader), then the string
    xnb_bytes = b"XNBw\x05\x00" + struct.pack("<I", 10 + len(body)) + body
    f = tmp_path / "ObjectInformation.xnb"
    f.write_bytes(xnb_bytes)
    assert xnb.load(f) == {"0": "Weeds/0/-1/Basic", "388": "Wood/2/-300/Basic"}


def test_xnb_rejects_non_xnb(tmp_path):
    f = tmp_path / "x.xnb"
    f.write_bytes(b"NOPE")
    with pytest.raises(ValueError):
        xnb.payload(f)


# ── Unity helpers ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [("CON", "_CON"), ("nul.png", "_nul.png"), ("a/b:c", "a_b_c"),
                                          ("trailing. ", "trailing"), ("", "unnamed")])
def test_safe_names(raw, expected):
    assert unity.safe(raw) == expected


def test_classes_to_csv_flattens_and_skips_ui(tmp_path):
    mono = tmp_path / "extract/mono"
    (mono / "Topics").mkdir(parents=True)
    (mono / "Image").mkdir(parents=True)
    topic = {"m_GameObject": {"m_FileID": 0, "m_PathID": 1}, "m_Name": "topics",
             "topic": {"name": "Sports", "weights": [1.0, 0.6]}, "icon": {"m_FileID": 0, "m_PathID": 5}}
    (mono / "Topics/topics_1.json").write_text(json.dumps(topic), encoding="utf-8")
    (mono / "Image/img_2.json").write_text(json.dumps({"m_Name": "x", "a": 1}), encoding="utf-8")
    assert unity.classes_to_csv(tmp_path) == 1
    text = (tmp_path / "reports/classes/Topics.csv").read_text(encoding="utf-8-sig")
    assert "topic.name" in text and "Sports" in text and "1.0;0.6" in text
    assert "m_GameObject" not in text and "icon" not in text
    assert not (tmp_path / "reports/classes/Image.csv").exists()


# ── engine detection ────────────────────────────────────────────────────────

def test_detect_engines(tmp_path):
    (tmp_path / "apk").mkdir()
    with zipfile.ZipFile(tmp_path / "apk/base.apk", "w") as z:
        z.writestr("lib/arm64-v8a/libgodot_android.so", b"")
    (tmp_path / "unpacked/assets/bin/Data").mkdir(parents=True)
    (tmp_path / "unpacked/assets/www").mkdir(parents=True)
    (tmp_path / "unpacked/assets/www/index.html").write_text("<html>")
    assert pipeline.detect(tmp_path) == ["godot", "html5", "unity"]


def test_detect_unknown(tmp_path):
    assert pipeline.detect(tmp_path) == ["unknown"]
