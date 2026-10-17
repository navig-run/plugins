"""Sealed SVG exports — vault secrets inside an ordinary, viewable SVG image.

A vault export has to survive being emailed, synced and stored next to everything else,
and it should not announce itself as "the passwords file". So the export is a real SVG
(it renders as a small pixel-grid picture) carrying the secrets, encrypted, in its
``<metadata>``. The technique is the dormant *SVG Vault* project's, which stored a file
in plain base64; here the payload is always sealed.

Format (``version`` 2.0)::

    <svg …>  …the picture…
      <metadata id="svgVaultMetadata">
        <data id="svgVaultData" encoding="base64">  base64(AES-256-GCM ciphertext ‖ tag)  </data>
        <info id="svgVaultInfo" encoding="base64">  base64(info JSON)                     </info>
      </metadata>
    </svg>

    info      = {"format": "navig-vault-svg", "version": "2.0",
                 "kdf": {"name": "PBKDF2-SHA256", "iterations": N, "salt": b64},
                 "cipher": "AES-256-GCM", "nonce": b64}
    key       = PBKDF2-HMAC-SHA256(NFC(passphrase).strip(), salt, N, 32 bytes)
    aad       = the info bytes exactly as stored (so editing N, the salt or the nonce makes
                the file refuse to open instead of weakening it)
    plaintext = {"format": 1, "exported_at": …, "items": [vault entries]}

The info carries no label, count or name: without the passphrase the file says only that
a sealed payload exists. PBKDF2 (not scrypt/Argon2) because every browser's WebCrypto has
it, which is what lets ``opener.html`` open an export with nothing installed. Keep this
docstring and ``opener.html`` in step — they are two implementations of one format.

Version 1.0 is the original SVG Vault's plain carrier (one file, base64, readable by
anyone). It is read so old files can still be opened; it is never written.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import secrets
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone

FORMAT_NAME = "navig-vault-svg"
VERSION = "2.0"
ITERATIONS = 600_000  # OWASP 2023 for PBKDF2-HMAC-SHA256; matches crypto._PBKDF2_ITERS. Tests lower it.
MAX_ITERATIONS = 10_000_000  # a crafted file must not be able to hang the reader
MIN_PASSPHRASE = 14

# Lowercase without 0/o/1/l: easy to read aloud and to type, 32 symbols = 5 bits each.
PASSPHRASE_ALPHABET = "23456789abcdefghijkmnpqrstuvwxyz"
PASSPHRASE_GROUPS = 5
PASSPHRASE_GROUP_LEN = 5

META_ID = "svgVaultMetadata"
DATA_IDS = ("svgVaultData", "svgArchiveData")  # second: the original app's older ids
INFO_IDS = ("svgVaultInfo", "svgArchiveInfo")
_SVG_NS = "http://www.w3.org/2000/svg"
_CLOSE_RE = re.compile(r"</(?:[\w.-]+:)?svg\s*>\s*\Z")


class VaultSvgError(Exception):
    """The SVG is not a vault export, is damaged, or the passphrase is wrong."""


@dataclass
class Info:
    """What an SVG says about itself without a passphrase."""

    version: str
    sealed: bool
    iterations: int | None = None
    # legacy (plain) carriers only — a sealed export reveals none of these
    file_name: str | None = None
    mime_type: str | None = None
    size: int | None = None

    def public(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class Bundle:
    """A sealed export, opened."""

    items: list[dict]
    exported_at: str | None = None
    version: str = VERSION


@dataclass
class LegacyFile:
    """A version 1.0 plain carrier: one file, readable by anyone."""

    file_name: str
    mime_type: str
    size: int
    data: bytes = field(repr=False)
    version: str = "1.0"


# ── passphrases ─────────────────────────────────────────────────────────────


def generate_passphrase() -> str:
    """A random passphrase of ~125 bits, e.g. ``k7mq4-x3vtb-…`` (5 groups of 5)."""
    groups = (
        "".join(secrets.choice(PASSPHRASE_ALPHABET) for _ in range(PASSPHRASE_GROUP_LEN))
        for _ in range(PASSPHRASE_GROUPS)
    )
    return "-".join(groups)


def normalize_passphrase(passphrase: str) -> str:
    """NFC + trimmed — ``opener.html`` applies exactly the same two steps."""
    return unicodedata.normalize("NFC", passphrase).strip()


def check_passphrase(passphrase: str) -> None:
    """Refuse a chosen passphrase too short to resist offline guessing.

    Whoever holds the file can guess forever with no lockout, so length is the only
    defence a chosen passphrase has. Generated ones always pass.
    """
    if len(normalize_passphrase(passphrase)) < MIN_PASSPHRASE:
        raise VaultSvgError(
            f"a passphrase needs at least {MIN_PASSPHRASE} characters — anyone holding the "
            "file can guess offline without limit (or let navig generate one)"
        )


def _derive(passphrase: str, salt: bytes, iterations: int) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
    return kdf.derive(normalize_passphrase(passphrase).encode("utf-8"))


# ── the picture ─────────────────────────────────────────────────────────────


def cover_svg(seed: bytes) -> str:
    """A deterministic 400×400 pixel-grid picture derived from *seed*.

    The original generator printed the file name on the image; a vault export carries no
    text at all, so the picture says nothing about what is inside.
    """
    size, cell = 400, 20
    cols = rows = size // cell
    stream = b""
    counter = 0
    while len(stream) * 2 < 12 + cols * rows:
        stream += hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
        counter += 1
    nibbles = stream.hex()
    palette = [f"#{nibbles[i:i + 6]}" for i in range(0, 24, 6)]
    cells = nibbles[24:]
    parts = [
        f'<svg xmlns="{_SVG_NS}" width="{size}" height="{size}" viewBox="0 0 {size} {size}">',
        f'<rect width="{size}" height="{size}" fill="{palette[0]}"/>',
    ]
    for y in range(rows):
        for x in range(cols):
            value = int(cells[y * cols + x], 16)
            if value > 8:
                fill = palette[(value % 3) + 1]
                parts.append(f'<rect x="{x * cell}" y="{y * cell}" width="{cell}" height="{cell}" fill="{fill}"/>')
    parts.append("</svg>")
    return "\n".join(parts)


# ── parsing ─────────────────────────────────────────────────────────────────


def _parse(svg_text: str) -> ET.Element:
    # Entity declarations are the expansion-bomb vector, and a carrier never needs one.
    # A bare DOCTYPE (Illustrator/Inkscape add them) is harmless: expat loads no external DTD.
    if "<!ENTITY" in svg_text.upper():
        raise VaultSvgError("this SVG declares XML entities — refusing to read it")
    try:
        root = ET.fromstring(svg_text)
    except ET.ParseError as exc:
        raise VaultSvgError(f"not a valid SVG ({exc})") from None
    if root.tag not in (f"{{{_SVG_NS}}}svg", "svg"):
        raise VaultSvgError("not an SVG image")
    return root


def _by_id(root: ET.Element, ids: tuple[str, ...]) -> ET.Element | None:
    found = {el.get("id"): el for el in root.iter() if el.get("id") in ids}
    for i in ids:
        if i in found:
            return found[i]
    return None


def _b64(text: str | None, what: str) -> bytes:
    try:
        return base64.b64decode("".join((text or "").split()), validate=True)
    except (binascii.Error, ValueError):
        raise VaultSvgError(f"the {what} block is not valid base64 — the file is damaged") from None


def _read(svg_text: str) -> tuple[bytes, dict, bytes]:
    """(info bytes, info dict, payload bytes) — or VaultSvgError("not a vault export")."""
    root = _parse(svg_text)
    data_el, info_el = _by_id(root, DATA_IDS), _by_id(root, INFO_IDS)
    if data_el is None or info_el is None:
        raise VaultSvgError("this SVG holds no vault export")
    info_raw = _b64(info_el.text, "info")
    try:
        info = json.loads(info_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise VaultSvgError("the info block is not readable — the file is damaged") from None
    if not isinstance(info, dict):
        raise VaultSvgError("the info block is not readable — the file is damaged")
    return info_raw, info, _b64(data_el.text, "data")


def _kdf_params(info: dict) -> tuple[bytes, int, bytes]:
    if info.get("format") != FORMAT_NAME or info.get("version") != VERSION:
        raise VaultSvgError(f"unsupported export version {info.get('version')!r} — update navig-vault")
    kdf = info.get("kdf") or {}
    if kdf.get("name") != "PBKDF2-SHA256" or info.get("cipher") != "AES-256-GCM":
        raise VaultSvgError("unsupported encryption parameters — update navig-vault")
    iterations = kdf.get("iterations")
    if not isinstance(iterations, int) or not 1 <= iterations <= MAX_ITERATIONS:
        raise VaultSvgError("the export's key parameters are out of range — the file is damaged")
    return _b64(kdf.get("salt"), "salt"), iterations, _b64(info.get("nonce"), "nonce")


def inspect(svg_text: str) -> Info | None:
    """Describe an SVG's payload without a passphrase; ``None`` when it carries none."""
    try:
        _info_raw, info, payload = _read(svg_text)
    except VaultSvgError as exc:
        if "holds no vault export" in str(exc):
            return None
        raise
    if info.get("format") == FORMAT_NAME:
        kdf = info.get("kdf") or {}
        return Info(version=str(info.get("version")), sealed=True, iterations=kdf.get("iterations"))
    return Info(
        version=str(info.get("version") or "1.0"),
        sealed=False,
        file_name=info.get("fileName"),
        mime_type=info.get("mimeType"),
        size=info.get("size") if isinstance(info.get("size"), int) else len(payload),
    )


def open_legacy(svg_text: str) -> LegacyFile:
    """Read a version 1.0 plain carrier (the original SVG Vault web app's format)."""
    _info_raw, info, payload = _read(svg_text)
    if info.get("format") == FORMAT_NAME:
        raise VaultSvgError("this is a sealed vault export — open it with its passphrase")
    size = info.get("size")
    if isinstance(size, int) and size != len(payload):
        raise VaultSvgError(f"the embedded file is {len(payload)} bytes but says {size} — the file is damaged")
    digest = info.get("sha256")
    if digest and hashlib.sha256(payload).hexdigest() != digest:
        raise VaultSvgError("the embedded file does not match its checksum — the file is damaged")
    return LegacyFile(
        file_name=str(info.get("fileName") or "embedded-file"),
        mime_type=str(info.get("mimeType") or "application/octet-stream"),
        size=len(payload),
        data=payload,
    )


def open_svg(svg_text: str, passphrase: str) -> Bundle:
    """Decrypt a sealed export. Wrong passphrase and tampering are indistinguishable by design."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    info_raw, info, payload = _read(svg_text)
    if info.get("format") != FORMAT_NAME:
        raise VaultSvgError("this SVG carries an unencrypted file, not a vault export")
    salt, iterations, nonce = _kdf_params(info)
    key = _derive(passphrase, salt, iterations)
    try:
        plain = AESGCM(key).decrypt(nonce, payload, info_raw)
    except (InvalidTag, ValueError):
        raise VaultSvgError("wrong passphrase, or the file was altered") from None
    try:
        doc = json.loads(plain.decode("utf-8"))
        items = doc["items"]
        if not isinstance(items, list):
            raise TypeError
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
        raise VaultSvgError("the export decrypted but its contents are not readable") from None
    return Bundle(items=items, exported_at=doc.get("exported_at"), version=str(info.get("version")))


# ── writing ─────────────────────────────────────────────────────────────────


def _insert(cover: str, block: str) -> str:
    """Put *block* inside the root element, leaving every other byte of *cover* untouched."""
    root = _parse(cover)
    if _by_id(root, DATA_IDS + INFO_IDS + (META_ID,)) is not None:
        raise VaultSvgError("this cover already carries a payload — use a different picture")
    m = _CLOSE_RE.search(cover)
    if m:
        return cover[: m.start()] + block + cover[m.start():]
    stripped = cover.rstrip()
    if stripped.endswith("/>"):  # a self-closing root: <svg … />
        return stripped[:-2] + ">" + block + "</svg>" + cover[len(stripped):]
    raise VaultSvgError("could not find the end of the SVG root element")


def seal(items: list[dict], passphrase: str, *, cover: str | None = None,
         iterations: int | None = None) -> str:
    """Encrypt vault *items* into an SVG. *cover* is your own picture; default is generated."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not normalize_passphrase(passphrase):
        raise VaultSvgError("the passphrase must not be empty")
    n = iterations or ITERATIONS
    salt, nonce = os.urandom(16), os.urandom(12)
    info_raw = json.dumps({
        "format": FORMAT_NAME,
        "version": VERSION,
        "kdf": {"name": "PBKDF2-SHA256", "iterations": n, "salt": base64.b64encode(salt).decode("ascii")},
        "cipher": "AES-256-GCM",
        "nonce": base64.b64encode(nonce).decode("ascii"),
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    plain = json.dumps({
        "format": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "items": items,
    }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ct = AESGCM(_derive(passphrase, salt, n)).encrypt(nonce, plain, info_raw)
    block = (
        f'<metadata id="{META_ID}">'
        f'<data id="{DATA_IDS[0]}" encoding="base64">{base64.b64encode(ct).decode("ascii")}</data>'
        f'<info id="{INFO_IDS[0]}" encoding="base64">{base64.b64encode(info_raw).decode("ascii")}</info>'
        "</metadata>"
    )
    return _insert(cover if cover is not None else cover_svg(salt), block)
