"""Valve KeyValues — the two formats Steam stores on disk, no external deps.

* **Text KV** (`loads`): ``libraryfolders.vdf``, ``appmanifest_*.acf``,
  ``loginusers.vdf``. Quoted ``"key" "value"`` pairs and ``"key" { ... }`` nesting.
* **Binary shortcuts** (`read_binary` / `write_binary`): ``userdata/<id>/config/
  shortcuts.vdf`` — a length-less tagged binary map (0x00 object · 0x01 string ·
  0x02 int32 · 0x08 end-of-map). Round-trip stable so existing shortcuts survive.

Reference format mined from BoilR / NonSteamLaunchers; reimplemented here.
"""

from __future__ import annotations

import struct
import zlib
from collections import OrderedDict

# ── text KeyValues ───────────────────────────────────────────────────────────


def loads(text: str) -> dict:
    """Parse text KeyValues into nested dicts. Tolerant: ignores // comments and
    stray whitespace; later duplicate keys win (Steam's own behaviour)."""
    tokens = _tokenize(text)
    pos = 0
    root: dict = {}
    # A file may have one or more top-level "key" { } blocks.
    while pos < len(tokens):
        key = tokens[pos]
        pos += 1
        if pos < len(tokens) and tokens[pos] == "{":
            value, pos = _parse_block(tokens, pos + 1)
            root[key] = value
        elif pos < len(tokens):
            root[key] = tokens[pos]
            pos += 1
    return root


def _parse_block(tokens: list[str], pos: int) -> tuple[dict, int]:
    obj: dict = {}
    while pos < len(tokens):
        tok = tokens[pos]
        if tok == "}":
            return obj, pos + 1
        key = tok
        pos += 1
        if pos < len(tokens) and tokens[pos] == "{":
            value, pos = _parse_block(tokens, pos + 1)
            obj[key] = value
        elif pos < len(tokens):
            obj[key] = tokens[pos]
            pos += 1
        else:
            break
    return obj, pos


def _tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
        elif c in "{}":
            tokens.append(c)
            i += 1
        elif c == '"':
            i += 1
            buf = []
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    nxt = text[i + 1]
                    buf.append({"n": "\n", "t": "\t", "\\": "\\", '"': '"'}.get(nxt, nxt))
                    i += 2
                else:
                    buf.append(text[i])
                    i += 1
            tokens.append("".join(buf))
            i += 1
        else:  # bare (unquoted) token
            buf = []
            while i < n and text[i] not in ' \t\r\n"{}':
                buf.append(text[i])
                i += 1
            tokens.append("".join(buf))
    return tokens


# ── binary shortcuts.vdf ─────────────────────────────────────────────────────

_OBJ, _STR, _INT, _END = 0x00, 0x01, 0x02, 0x08


def read_binary(data: bytes) -> "OrderedDict":
    """Parse a binary shortcuts.vdf into an ordered nested map. Returns the root
    map (typically ``{"shortcuts": {"0": {...}, ...}}``)."""
    root, _ = _read_map(data, 0)
    return root


def _read_map(data: bytes, pos: int) -> tuple["OrderedDict", int]:
    obj: OrderedDict = OrderedDict()
    while pos < len(data):
        tag = data[pos]
        pos += 1
        if tag == _END:
            return obj, pos
        key, pos = _read_cstr(data, pos)
        if tag == _OBJ:
            value, pos = _read_map(data, pos)
        elif tag == _STR:
            value, pos = _read_cstr(data, pos)
        elif tag == _INT:
            value = struct.unpack_from("<i", data, pos)[0]
            pos += 4
        else:
            raise ValueError(f"unknown VDF tag {tag:#x} at {pos - 1}")
        obj[key] = value
    return obj, pos


def _read_cstr(data: bytes, pos: int) -> tuple[str, int]:
    end = data.index(b"\x00", pos)
    return data[pos:end].decode("utf-8", "replace"), end + 1


def write_binary(root: dict) -> bytes:
    """Serialize a root map (with a ``shortcuts`` object) back to bytes."""
    return _write_map(root)


def _write_map(obj: dict) -> bytes:
    out = bytearray()
    for key, value in obj.items():
        kb = key.encode("utf-8") + b"\x00"
        if isinstance(value, dict):
            out += bytes([_OBJ]) + kb + _write_map(value)
        elif isinstance(value, bool):
            out += bytes([_INT]) + kb + struct.pack("<i", 1 if value else 0)
        elif isinstance(value, int):
            out += bytes([_INT]) + kb + struct.pack("<I", value & 0xFFFFFFFF)
        else:
            out += bytes([_STR]) + kb + str(value).encode("utf-8") + b"\x00"
    out += bytes([_END])
    return bytes(out)


def shortcut_appid(exe: str, app_name: str) -> int:
    """Steam's legacy 32-bit shortcut appid (grid-art key), high bit set.

    Must be computed from the exact ``exe`` string stored in the shortcut."""
    crc = zlib.crc32((exe + app_name).encode("utf-8")) & 0xFFFFFFFF
    return crc | 0x80000000
