"""XNA / MonoGame / FNA content (``.xnb``) — Stardew Valley and other XNA-era games.

Decodes LZ4- or uncompressed XNB (LZX is reported, not decoded): string/int/bool, Dictionary,
List and Array readers → JSON; Texture2D (Color, DXT1/3/5) → PNG; anything else (maps, fonts,
custom game classes) is kept as the raw decompressed payload. XACT wave banks go through vgmstream.
"""

from __future__ import annotations

import collections
import json
import re
import struct
from pathlib import Path


class _Reader:
    def __init__(self, d: bytes):
        self.d, self.p = d, 0

    def u8(self) -> int:
        self.p += 1
        return self.d[self.p - 1]

    def i32(self) -> int:
        self.p += 4
        return struct.unpack_from("<i", self.d, self.p - 4)[0]

    def v7(self) -> int:
        r = s = 0
        while True:
            b = self.u8()
            r |= (b & 0x7F) << s
            s += 7
            if not b & 0x80:
                return r

    def string(self) -> str:
        n = self.v7()
        self.p += n
        return self.d[self.p - n:self.p].decode("utf-8")


def _short(t: str) -> str:
    return re.sub(r"\[\[.*", "", t.split(",")[0]).split(".")[-1]


def _reader_for(t: str) -> str:
    base = t.split("[[")[0].split(",")[0].strip()
    simple = {"System.String": "StringReader", "System.Int32": "Int32Reader", "System.Boolean": "BooleanReader"}
    if base in simple:
        return simple[base]
    if "[[" in t:
        kind = base.split(".")[-1]
        return f"Microsoft.Xna.Framework.Content.{kind.replace('`', 'Reader`')}{t[t.index('[['):t.rindex(']]') + 2]}"
    if base.endswith("[]"):
        return f"Microsoft.Xna.Framework.Content.ArrayReader`1[[{base[:-2]}]]"
    return base


def _generic_args(tname: str) -> list[str]:
    """'X`2[[A, asm],[B[[C, asm]], asm]]' -> readers for A and B[[C]] (arguments sit at bracket depth 2)."""
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(tname):
        if ch == "[":
            depth += 1
            if depth == 2:
                start = i + 1
        elif ch == "]":
            if depth == 2:
                parts.append(tname[start:i])
            depth -= 1
    return [_reader_for(p) for p in parts]


def _is_value(t: str) -> bool:
    return t in ("Int32Reader", "BooleanReader")


def _value(r: _Reader, readers: list[str], tname, value_type: bool):
    if not value_type:
        idx = r.v7()
        if idx == 0:
            return None
        tname = readers[idx - 1]
    s = _short(tname)
    if s == "StringReader":
        return r.string()
    if s == "Int32Reader":
        return r.i32()
    if s == "BooleanReader":
        return bool(r.u8())
    if s.startswith("DictionaryReader`2"):
        k, v = _generic_args(tname)
        return {str(_value(r, readers, k, _is_value(k))): _value(r, readers, v, _is_value(v)) for _ in range(r.i32())}
    if s.startswith("ListReader`1") or s.startswith("ArrayReader`1"):
        (a,) = _generic_args(tname)
        return [_value(r, readers, a, _is_value(a)) for _ in range(r.i32())]
    raise ValueError(f"unsupported reader {s}")


def payload(path: Path) -> bytes:
    b = path.read_bytes()
    if b[:3] != b"XNB":
        raise ValueError("not an XNB file")
    if b[5] & 0x40:
        import lz4.block
        return lz4.block.decompress(b[14:], uncompressed_size=struct.unpack_from("<I", b, 10)[0])
    if b[5] & 0x80:
        raise ValueError("LZX compression not supported")
    return b[10:]


def _header(d: bytes) -> tuple[_Reader, list[str]]:
    r = _Reader(d)
    readers = []
    for _ in range(r.v7()):
        readers.append(r.string())
        r.i32()
    r.v7()  # shared resources
    return r, readers


def load(path: Path):
    """Decode a table-like XNB (Dictionary/List/string…) into Python data."""
    r, readers = _header(payload(path))
    return _value(r, readers, None, False)


def texture(d: bytes):
    from PIL import Image

    r, _ = _header(d)
    r.v7()  # object type index
    fmt, w, h, _mips = struct.unpack_from("<iIII", d, r.p)
    r.p += 16
    size = struct.unpack_from("<I", d, r.p)[0]
    data = d[r.p + 4:r.p + 4 + size]
    if fmt == 0:
        return Image.frombytes("RGBA", (w, h), data)
    if fmt in (4, 5, 6):
        return Image.frombytes("RGBA", (w, h), data, "bcn", {4: 1, 5: 2, 6: 3}[fmt])
    raise ValueError(f"texture format {fmt}")


def is_xnb_game(game: Path) -> bool:
    content = game / "unpacked/assets/Content"
    return content.exists() and next(content.rglob("*.xnb"), None) is not None


def export(content: Path, out: Path, skip: tuple[str, ...] = ("SaveGames",)) -> dict:
    """Every XNB under ``content``: textures → out/textures/*.png, tables → out/text/*.json, rest → out/raw-xnb/."""
    counts, errors = collections.Counter(), collections.Counter()
    for f in sorted(content.rglob("*.xnb")):
        rel = f.relative_to(content).with_suffix("")
        if rel.parts and rel.parts[0] in skip:
            continue
        try:
            d = payload(f)
            r, readers = _header(d)
            kind = _short(readers[0]) if readers else ""
            if kind.startswith("Texture2DReader"):
                dest = out / "textures" / rel.with_suffix(".png")
                dest.parent.mkdir(parents=True, exist_ok=True)
                texture(d).save(dest)
                counts["texture -> png"] += 1
                continue
            try:
                value = _value(r, readers, None, False)
                dest = out / "text" / rel.with_suffix(".json")
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
                counts["table -> json"] += 1
            except Exception:
                dest = out / "raw-xnb" / rel.with_suffix(".bin")
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(d)
                counts[f"raw ({kind.split('`')[0] or 'unknown'})"] += 1
        except Exception as exc:
            errors[f"{type(exc).__name__}: {str(exc)[:60]}"] += 1
    return {"exported": dict(counts), "errors": dict(errors)}
