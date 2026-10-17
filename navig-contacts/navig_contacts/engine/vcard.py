"""
vCard 3.0 parser, written against what this archive actually contains.

Not a general-purpose vCard library: it handles exactly the dialects found in
``G:\\Backups\\Address Book`` — Google Contacts, Apple/iOS, and Skype exports —
namely CRLF line endings, RFC-2425 folding, ``itemN.``-grouped properties with
``X-ABLabel``, backslash-escaped values (the URLs are stored as ``http\\://``),
quoted-printable and base64 parameters, and Skype's ``X-SKYPE-*`` extensions.

Cards are yielded with their original text attached so every downstream
decision stays traceable to the bytes it came from.
"""
from __future__ import annotations

import base64
import binascii
import quopri
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

#: Encodings to try, in order.  The archive is UTF-8 throughout, but older
#: Windows exports of the same address book were cp1251, and a decode failure
#: must not lose a file.
_ENCODINGS = ("utf-8", "cp1251", "latin-1")

_BEGIN = "BEGIN:VCARD"
_END = "END:VCARD"

#: A folded continuation line starts with one space or tab (RFC 2425 §5.8.1).
_FOLD_RE = re.compile(r"\r?\n[ \t]")
#: `item1.EMAIL;TYPE=INTERNET` -> group prefix, property name, params
_LINE_RE = re.compile(r"^(?:(item\d+)\.)?([A-Za-z0-9\-]+)((?:;[^:]*)?)$")


def _decode(data: bytes) -> str:
    for enc in _ENCODINGS:
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _unescape(value: str) -> str:
    """Undo vCard backslash escaping: \\n \\, \\; \\: \\\\ ."""
    out: list[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append("\n" if nxt in ("n", "N") else nxt)
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _split_property(line: str) -> Optional[tuple[str, str, str, str]]:
    """
    Split one unfolded line into ``(group, name, params, value)``.

    The separator is the first *unescaped* colon: values legitimately contain
    ``\\:`` (Google wrote every URL that way), and splitting on the first colon
    of any kind would truncate them to ``http``.
    """
    idx = -1
    i = 0
    while i < len(line):
        if line[i] == "\\":
            i += 2
            continue
        if line[i] == ":":
            idx = i
            break
        i += 1
    if idx <= 0:
        return None
    head, value = line[:idx], line[idx + 1:]
    m = _LINE_RE.match(head)
    if not m:
        return None
    group, name, params = m.group(1) or "", m.group(2).upper(), m.group(3)
    return group, name, params.lstrip(";"), value


def _decode_value(value: str, params: str) -> str:
    """Apply ENCODING=QUOTED-PRINTABLE / CHARSET before unescaping."""
    p = params.upper()
    if "QUOTED-PRINTABLE" in p:
        charset = "utf-8"
        m = re.search(r"CHARSET=([A-Za-z0-9\-_]+)", params, re.I)
        if m:
            charset = m.group(1)
        try:
            value = quopri.decodestring(value.encode("latin-1")).decode(
                charset, errors="replace"
            )
        except Exception:
            pass
    return _unescape(value)


def _decode_photo(value: str, params: str) -> Optional[bytes]:
    """
    Decode an inline ``PHOTO;ENCODING=B`` payload to image bytes.

    Returns None rather than raising for anything that is not a decodable
    image: a URI-valued PHOTO, a truncated export, a corrupt card.  One bad
    avatar must not cost the contact it belongs to.
    """
    p = params.upper()
    if "ENCODING=B" not in p and "BASE64" not in p:
        return None  # a PHOTO;VALUE=URI, not embedded data
    payload = "".join(value.split())
    if not payload:
        return None
    try:
        # Exports routinely drop the "=" padding when folding lines.
        data = base64.b64decode(payload + "=" * (-len(payload) % 4))
    except (binascii.Error, ValueError):
        return None
    # Length alone is not evidence of an image: one card in this archive
    # base64-decodes to 4 KB of something Pillow cannot open.  Check the
    # magic bytes instead.
    return data if _is_image(data) else None


def _is_image(data: bytes) -> bool:
    """True for JPEG / PNG / GIF / BMP / WEBP magic bytes."""
    if len(data) < 100:
        return False
    jpeg = bytes([0xFF, 0xD8, 0xFF])
    png = bytes([0x89]) + b"PNG" + bytes([0x0D, 0x0A, 0x1A, 0x0A])
    return (
        data[:3] == jpeg
        or data[:8] == png
        or data[:6] in (b"GIF87a", b"GIF89a")
        or data[:2] == b"BM"
        or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
    )


@dataclass
class VCard:
    """One parsed card.  ``raw`` is the original text, kept for provenance."""

    source_file: str
    card_index: int
    raw: str
    fn: str = ""
    surname: str = ""
    given: str = ""
    tels: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    skype: Optional[str] = None
    skype_country: Optional[str] = None
    skype_city: Optional[str] = None
    skype_language: Optional[str] = None
    org: Optional[str] = None
    title: Optional[str] = None
    bday: Optional[str] = None
    note: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = None
    has_photo: bool = False
    #: The decoded avatar, when the card carried an inline base64 one.
    #: 309 of the archive's cards do — Skype embedded the profile picture
    #: straight into the vCard, and it is the only copy of those faces.
    photo_bytes: Optional[bytes] = None

    @property
    def display_name(self) -> str:
        """FN if present, else the N components joined, else ''."""
        if self.fn.strip():
            return self.fn.strip()
        joined = " ".join(x for x in (self.given, self.surname) if x).strip()
        return joined


def parse_card(raw: str, source_file: str, card_index: int) -> VCard:
    """Parse one card body (the text between BEGIN:VCARD and END:VCARD)."""
    card = VCard(source_file=source_file, card_index=card_index, raw=raw)
    unfolded = _FOLD_RE.sub("", raw.replace("\r\n", "\n"))

    for line in unfolded.split("\n"):
        line = line.rstrip()
        if not line:
            continue
        parts = _split_property(line)
        if parts is None:
            continue
        _group, name, params, rawval = parts

        if name == "PHOTO":
            card.has_photo = True
            card.photo_bytes = _decode_photo(rawval, params)
            continue

        value = _decode_value(rawval, params)
        if not value.strip() and name not in ("N",):
            continue

        if name == "FN":
            if not card.fn:
                card.fn = value.strip()
        elif name == "N":
            comps = value.split(";")
            card.surname = comps[0].strip() if len(comps) > 0 else ""
            card.given = comps[1].strip() if len(comps) > 1 else ""
        elif name == "TEL":
            card.tels.append(value.strip())
        elif name == "X-SKYPE-PSTNNUMBER":
            card.tels.append(value.strip())
        elif name == "EMAIL":
            card.emails.append(value.strip())
        elif name == "URL":
            card.urls.append(value.strip())
        elif name in ("X-SOCIALPROFILE", "IMPP"):
            card.urls.append(value.strip())
        elif name == "X-SKYPE-USERNAME":
            card.skype = card.skype or value.strip()
        elif name == "X-SKYPE-COUNTRY":
            card.skype_country = card.skype_country or value.strip()
        elif name == "X-SKYPE-CITY":
            card.skype_city = card.skype_city or value.strip()
        elif name == "X-SKYPE-LANGUAGE":
            card.skype_language = card.skype_language or value.strip()
        elif name == "ORG":
            card.org = card.org or value.split(";")[0].strip() or None
        elif name == "TITLE":
            card.title = card.title or value.strip() or None
        elif name == "BDAY":
            card.bday = card.bday or value.strip() or None
        elif name == "NOTE":
            card.note = card.note or value.strip() or None
        elif name == "ADR":
            # po;ext;street;locality;region;postal;country
            comps = [c.strip() for c in value.split(";")]
            if len(comps) >= 7:
                card.city = card.city or (comps[3] or None)
                card.country = card.country or (comps[6] or None)

    return card


def iter_cards(path: Path) -> Iterator[VCard]:
    """Yield every card in one .vcf file, in file order."""
    text = _decode(path.read_bytes())
    source_file = path.name
    index = 0
    for chunk in text.split(_BEGIN)[1:]:
        end = chunk.find(_END)
        body = chunk[:end] if end != -1 else chunk
        index += 1
        yield parse_card(body, source_file, index)


def iter_vcf_files(target: Path) -> list[Path]:
    """One .vcf file, or every .vcf/.vcard in a directory, sorted by name."""
    if target.is_dir():
        found = sorted(
            p for p in target.iterdir()
            if p.is_file() and p.suffix.lower() in (".vcf", ".vcard")
        )
        return found
    return [target]


def parse_target(target: Path) -> list[VCard]:
    """Parse every card under a file or directory."""
    cards: list[VCard] = []
    for path in iter_vcf_files(target):
        cards.extend(iter_cards(path))
    return cards
