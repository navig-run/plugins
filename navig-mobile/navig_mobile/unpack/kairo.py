"""Kairosoft ``.bin`` archives (Game Dev Story, Department Store, … Unity ports).

Layout after XOR with a per-title repeating key:
  u32 header_len, u32 data_len, u32 count, count x (u32 name_len, name), count x u32 offset, count x u32 size,
  data area at header_len + 4: per entry at offset -> u32 size, payload (zlib if it starts 78 9C).
Keys seen: ``00 11 22 .. ff`` (Game Dev Story 3), ``cd 51 05 4f`` (Department Store). Titles using a
non-repeating keystream (Ramen/Noodle) are reported as undecodable.
"""

from __future__ import annotations

import collections
import struct
import zlib
from pathlib import Path

KNOWN_KEYS = [bytes(range(0, 256, 17)), bytes.fromhex("cd51054f")]


def plausible(d: bytes) -> bool:
    if len(d) < 20:
        return False
    hdr, _total, count = struct.unpack_from(">III", d, 0)
    (n,) = struct.unpack_from(">I", d, 12)
    return 0 < count < 5000 and hdr < len(d) and 0 < n < 200 and d[16:16 + n].isascii()


def guess_key(blobs: list[bytes], length: int) -> bytes:
    """Archive headers are mostly zero bytes, so the commonest byte per key slot is the key byte."""
    return bytes(collections.Counter(b for f in blobs for i, b in enumerate(f[:4096]) if i % length == j).most_common(1)[0][0]
                 for j in range(length))


def decode(b: bytes, keys: list[bytes]) -> bytes:
    if plausible(b):
        return b
    for key in keys:
        d = bytes(x ^ key[i % len(key)] for i, x in enumerate(b))
        if plausible(d):
            return d
    raise ValueError("unknown obfuscation (not a repeating XOR key)")


def unpack_archive(data: bytes, out: Path) -> int:
    hdr, _total, count = struct.unpack_from(">III", data, 0)
    pos, names = 12, []
    for _ in range(count):
        (n,) = struct.unpack_from(">I", data, pos)
        names.append(data[pos + 4:pos + 4 + n].decode("utf-8", "replace"))
        pos += 4 + n
    offsets = struct.unpack_from(f">{count}I", data, pos)
    sizes = struct.unpack_from(f">{count}I", data, pos + 4 * count)
    base = hdr + 4
    for name, off, size in zip(names, offsets, sizes):
        p = base + off
        n = struct.unpack_from(">I", data, p)[0]
        if n != size:
            raise ValueError(f"{name}: size mismatch {n} != {size}")
        payload = data[p + 4:p + 4 + n]
        if payload[:2] == b"\x78\x9c":
            payload = zlib.decompress(payload)
        target = out / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    return count


def unpack_dir(src: Path, dst: Path) -> dict[str, str]:
    """Unpack every ``*.bin`` in ``src`` into ``dst/<archive>/``. Returns per-archive results."""
    files = sorted(src.glob("*.bin"))
    blobs = [f.read_bytes() for f in files]
    keys = KNOWN_KEYS + [guess_key(blobs, n) for n in (4, 8, 16, 32)] if blobs else KNOWN_KEYS
    results = {}
    for f, blob in zip(files, blobs):
        try:
            results[f.name] = f"{unpack_archive(decode(blob, keys), dst / f.stem)} files"
        except Exception as exc:
            results[f.name] = f"FAILED: {exc}"
    return results


def is_kairosoft(game: Path) -> bool:
    return (game / "extract/text/kairolib.bin").exists()
