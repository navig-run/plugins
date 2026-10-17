"""Portable backups: one encrypted file that restores on any machine, with a passphrase.

A machine-mode cabinet cannot be opened on another computer, and a reinstall changes
the machine. So a cabinet without a backup is one disk failure from gone; this module
is the other half of the design, not an extra.

Format (``.ncab``)::

    b"NCABBAK1" ‖ u32 len(header) ‖ header (JSON: scrypt params + salt)
    ‖ stream.py stream, key = scrypt(passphrase), context = b"navig-cabinet/backup/" ‖ sha256(header)

and the stream's plaintext is an ordinary **tar** file::

    manifest.json               every item's metadata (titles, tags, expiry, OCR text)
    vault/secrets.json          (only with --with-vault) every vault secret — see vault_bridge
    files/<id>/<original name>  every item's original bytes

Binding the header's hash into the stream context means editing the KDF parameters
does not produce a weaker key — it produces a stream that will not decrypt.

The tar is the escape hatch: ``recover_backup.py`` (stdlib + ``cryptography`` only)
turns a bundle back into that tar, which any archive tool opens. The documents never
depend on navig still existing.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import struct
import tarfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from . import keys
from .store import Cabinet, Duplicate, Item, safe_filename
from .stream import StreamDecryptor, StreamEncryptor, StreamError

from navig_sdk.host import command_name  # noqa: E402

# The command a user types here: `navig cabinet` inside navig, `navig-cabinet` on its own.
CMD = command_name("cabinet", standalone="navig-cabinet")

MAGIC = b"NCABBAK1"
_LEN = struct.Struct(">I")
BACKUP_N = 2**17  # same cost as a passphrase-mode cabinet; tests lower it
VAULT_PATH = "vault/secrets.json"


class BackupError(Exception):
    pass


@dataclass
class RestoreStats:
    restored: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    vault_entries: list[dict] | None = None  # present when the backup carries the vault


def _context(header: bytes) -> bytes:
    return b"navig-cabinet/backup/" + hashlib.sha256(header).digest()


def write_backup(cabinet: Cabinet, items: list[Item], out: BinaryIO, passphrase: str,
                 vault_entries: list[dict] | None = None) -> int:
    """Stream *items* (and, if given, the vault's secrets) into an encrypted bundle on *out*.

    Returns the item count. *vault_entries* come from ``vault_bridge.export_vault`` and
    exist only inside the encrypted stream.
    """
    if not passphrase:
        raise BackupError("a backup needs a passphrase")
    salt = os.urandom(16)
    header = json.dumps({
        "format": 1,
        "kdf": {"name": "scrypt", "n": BACKUP_N, "r": keys.SCRYPT_R, "p": keys.SCRYPT_P,
                "salt": base64.b64encode(salt).decode("ascii")},
    }, sort_keys=True).encode("utf-8")
    key = keys.derive(passphrase.encode("utf-8"), salt, BACKUP_N)
    out.write(MAGIC + _LEN.pack(len(header)) + header)

    enc = StreamEncryptor(out, key, _context(header))
    manifest = []
    for it in items:
        entry = it.public(with_text=True)
        entry["path"] = f"files/{it.id}/{safe_filename(it.original_name)}"
        manifest.append(entry)
    doc: dict = {"format": 1, "items": manifest}
    vault_body = b""
    if vault_entries is not None:
        vault_body = json.dumps({"format": 1, "items": vault_entries}, ensure_ascii=False).encode("utf-8")
        doc["vault"] = {"path": VAULT_PATH, "items": len(vault_entries)}
    body = json.dumps(doc, ensure_ascii=False, indent=1).encode("utf-8")

    with tarfile.open(fileobj=enc, mode="w|", format=tarfile.PAX_FORMAT) as tar:
        info = tarfile.TarInfo("manifest.json")
        info.size, info.mode = len(body), 0o600
        tar.addfile(info, io.BytesIO(body))
        if vault_entries is not None:
            info = tarfile.TarInfo(VAULT_PATH)
            info.size, info.mode = len(vault_body), 0o600
            tar.addfile(info, io.BytesIO(vault_body))
        for it, entry in zip(items, manifest):
            info = tarfile.TarInfo(entry["path"])
            info.size, info.mode = it.size, 0o600
            try:
                info.mtime = int(datetime.fromisoformat(it.added_at).timestamp())
            except ValueError:
                pass
            with cabinet.open_stream(it.id) as dec:
                tar.addfile(info, dec)
                dec.read_to_end()  # prove the final chunk authenticated
            if dec.sha256 != it.sha256:
                raise BackupError(f"{it.id}: content does not match its checksum — backup aborted")
    enc.close()
    return len(items)


def _open_bundle(src: BinaryIO, passphrase: str) -> StreamDecryptor:
    if src.read(len(MAGIC)) != MAGIC:
        raise BackupError(f"not a {CMD} backup")
    raw = src.read(_LEN.size)
    if len(raw) < _LEN.size:
        raise BackupError("backup is truncated")
    (hlen,) = _LEN.unpack(raw)
    if hlen > 64 * 1024:
        raise BackupError("backup header is implausibly large")
    header = src.read(hlen)
    try:
        kdf = json.loads(header.decode("utf-8"))["kdf"]
        salt = base64.b64decode(kdf["salt"])
        key = keys.derive(passphrase.encode("utf-8"), salt, int(kdf["n"]), int(kdf["r"]), int(kdf["p"]))
    except (ValueError, KeyError, TypeError) as exc:
        raise BackupError(f"backup header is unreadable: {exc}") from exc
    return StreamDecryptor(src, key, _context(header))


def restore_backup(cabinet: Cabinet, src: BinaryIO, passphrase: str) -> RestoreStats:
    """Add every item in the bundle that the cabinet does not already hold.

    Vault secrets, when the bundle carries them, are returned in ``vault_entries`` for
    the caller to restore (``vault_bridge.import_vault``) — this module never writes the
    vault itself.
    """
    st = RestoreStats()
    dec = _open_bundle(src, passphrase)
    by_path: dict[str, dict] = {}
    try:
        with tarfile.open(fileobj=dec, mode="r|") as tar:
            for member in tar:
                if member.name == "manifest.json":
                    fh = tar.extractfile(member)
                    data = json.loads(fh.read().decode("utf-8")) if fh else {}
                    by_path = {e["path"]: e for e in data.get("items", [])}
                    continue
                if member.name == VAULT_PATH:
                    fh = tar.extractfile(member)
                    data = json.loads(fh.read().decode("utf-8")) if fh else {}
                    st.vault_entries = list(data.get("items", []))
                    continue
                if not member.isfile():
                    continue
                meta = by_path.get(member.name)
                if meta is None:
                    st.errors.append(f"{member.name}: not in the manifest — skipped")
                    continue
                fh = tar.extractfile(member)
                if fh is None:
                    continue
                try:
                    cabinet.add_stream(
                        fh, original_name=meta["original_name"], kind=meta["kind"],
                        mime=meta["mime"], category=meta["category"], title=meta["title"],
                        tags=meta.get("tags") or [], expires=meta.get("expires"),
                        issuer=meta.get("issuer"), notes=meta.get("notes"),
                        text=meta.get("text") or "", text_source=meta.get("text_source") or "",
                        expires_detected=bool(meta.get("expires_detected")),
                        expected_sha256=meta["sha256"], added_at=meta.get("added_at"),
                    )
                    st.restored += 1
                except Duplicate:
                    st.skipped += 1
        dec.read_to_end()  # the tar end-marker is not the stream end; verify the last chunk
    except StreamError as exc:
        if st.restored == 0 and st.skipped == 0:
            raise BackupError("wrong passphrase, or the backup is damaged") from exc
        raise BackupError(f"backup is damaged after {st.restored + st.skipped} item(s): {exc}") from exc
    except tarfile.TarError as exc:
        raise BackupError(f"backup archive is damaged: {exc}") from exc
    return st


def recovery_script() -> Path:
    """Where the standalone recovery script ships (it is also printed in the README)."""
    return Path(__file__).with_name("recover_backup.py")
