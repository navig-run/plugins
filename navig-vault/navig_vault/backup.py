"""Recipient-key vault backups — written unattended, restorable only offline.

A backup holds every item exactly as stored (DEK sealed under the VMK, payload
sealed under its DEK) plus the VMK sealed to the vault's RECOVERY public key.
Writing one needs no human: the daemon has the VMK and the public key, never the
private recovery key. Restoring needs that private key — which is printed once
and kept offline — so a stolen backup file is useless on its own.

Restoring into a new directory re-wraps the same VMK for the TARGET machine
(its own device + fingerprint slots, plus the same recovery slot): this is how a
vault moves to another machine without ever being decrypted item by item, and
without the source machine's fingerprint.

File: one JSON document, owner-only, written atomically. ``mac`` is an
HMAC-SHA256 over the canonical body under a key derived from the VMK, so a
swapped label, metadata edit or dropped item is detected on restore — the
per-item AES-GCM tags alone would not catch a blob moved to another label.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path

from .crypto import CryptoEngine, CryptoError
from .keyslots import RECOVERY, parse_recovery_key

__all__ = ["BackupError", "FORMAT", "read_backup", "restore_backup", "write_backup"]

FORMAT = "navig-vault-backup/1"
_VMK_INFO = b"navig-vault-backup/v1"
_ITEM_FIELDS = (
    "id", "kind", "label", "provider", "encrypted_dek", "encrypted_blob",
    "metadata_json", "created_at", "updated_at", "version",
)
_BLOB_FIELDS = ("encrypted_dek", "encrypted_blob")


class BackupError(RuntimeError):
    """The backup cannot be written, read, verified or restored (never carries a value)."""


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def _mac_key(vmk: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes  # noqa: PLC0415
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF  # noqa: PLC0415

    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"navig-vault-backup-mac/v1").derive(vmk)


def _canonical(body: dict) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def write_backup(vault, path: Path) -> dict:
    """Write a backup of *vault* to *path*. Returns a summary (no values).

    Requires a recovery keyslot — without one there is nobody who could ever
    restore the file, and writing it would only pretend to be a backup.
    """
    recovery = [s for s in vault.keyring.slots() if s.kind == RECOVERY]
    if not recovery:
        raise BackupError("no recovery keyslot — run `navig vault recovery init` first")
    public = bytes.fromhex(recovery[0].params["public"])
    vmk = vault._master_key()

    with vault._store._lock:
        rows = vault._store._connect().execute(
            f"SELECT {', '.join(_ITEM_FIELDS)} FROM vault_items ORDER BY label"
        ).fetchall()
    items = []
    unreadable: list[str] = []
    for row in rows:
        # An item whose DEK does not open under the VMK was sealed under some other
        # key (an old passphrase session, a past fingerprint) — no holder of this
        # vault's key can ever read it, on any machine. Carrying it would make the
        # whole backup un-restorable (restore proves every item), so it is left out
        # and NAMED, never dropped silently.
        try:
            CryptoEngine.open(vmk, bytes(row["encrypted_dek"]))
        except CryptoError:
            unreadable.append(row["label"])
            continue
        entry = {f: row[f] for f in _ITEM_FIELDS}
        for f in _BLOB_FIELDS:
            entry[f] = _b64(bytes(entry[f]))
        items.append(entry)

    body = {
        "format": FORMAT,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": socket.gethostname(),
        "recovery_id": recovery[0].params.get("id", ""),
        "recovery_public": public.hex(),
        "wrapped_vmk": _b64(CryptoEngine.seal_to(public, vmk, _VMK_INFO)),
        "items": items,
    }
    body["mac"] = hmac.new(_mac_key(vmk), _canonical(body), hashlib.sha256).hexdigest()

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    from ._compat import set_owner_only_file_permissions  # noqa: PLC0415

    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(body, indent=1), encoding="utf-8")
    set_owner_only_file_permissions(tmp)
    os.replace(tmp, path)
    return {
        "path": str(path),
        "items": len(items),
        "recovery_id": body["recovery_id"],
        "skipped_unreadable": unreadable,
    }


def read_backup(path: Path, recovery_key: str) -> tuple[bytes, dict]:
    """Open and VERIFY a backup: returns ``(vmk, body)``.

    Every check that can fail without writing anything happens here: format,
    the recovery key, the MAC over the whole body, and every item's DEK opening
    under the VMK. Raises :class:`BackupError` on any of them.
    """
    try:
        body = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackupError(f"cannot read backup {Path(path).name} ({type(exc).__name__})") from exc
    if body.get("format") != FORMAT:
        raise BackupError(f"not a {FORMAT} file")
    try:
        private = parse_recovery_key(recovery_key)
    except ValueError as exc:
        raise BackupError(str(exc)) from exc
    if CryptoEngine.x25519_public(private).hex() != body.get("recovery_public"):
        raise BackupError("that recovery key does not belong to this backup")
    try:
        vmk = CryptoEngine.open_from(private, _unb64(body["wrapped_vmk"]), _VMK_INFO)
    except (CryptoError, KeyError, ValueError) as exc:
        raise BackupError("the backup's key does not open (corrupt or tampered)") from exc
    mac = body.pop("mac", "")
    expected = hmac.new(_mac_key(vmk), _canonical(body), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        raise BackupError("backup integrity check failed — the file was modified")
    body["mac"] = mac
    for entry in body["items"]:
        try:
            CryptoEngine.open(vmk, _unb64(entry["encrypted_dek"]))
        except CryptoError as exc:
            raise BackupError(f"item {entry['label']!r} does not open under the backup key") from exc
    return vmk, body


def restore_backup(path: Path, recovery_key: str, into_dir: Path) -> dict:
    """Restore a verified backup into an EMPTY vault directory. Returns a summary.

    The new vault gets this machine's device + fingerprint slots and the same
    recovery slot, then every item is decrypted once to prove the restore.
    Refuses a directory that already holds a vault with items or keyslots —
    restoring never merges into or overwrites a live vault.
    """
    vmk, body = read_backup(path, recovery_key)

    from .core import Vault  # noqa: PLC0415

    into_dir = Path(into_dir)
    target = Vault(into_dir)
    if target._store.count() > 0 or target.keyring.initialized():
        raise BackupError(f"{into_dir} already holds a vault — restore only into an empty directory")
    target.keyring.initialize(vmk)
    recovery_slot = target.keyring.wrap(
        RECOVERY, vmk, recipient_public=bytes.fromhex(body["recovery_public"])
    )
    with target._store.transaction() as conn:
        target.keyring._insert(conn, recovery_slot)
        for entry in body["items"]:
            conn.execute(
                f"INSERT INTO vault_items ({', '.join(_ITEM_FIELDS)}) VALUES "
                f"({', '.join('?' for _ in _ITEM_FIELDS)})",
                tuple(_unb64(entry[f]) if f in _BLOB_FIELDS else entry[f] for f in _ITEM_FIELDS),
            )
    restored = 0
    for entry in body["items"]:
        target.get_bytes(entry["label"])  # proves DEK + payload open on the TARGET
        restored += 1
    return {"items": restored, "vault_dir": str(into_dir), "source": body.get("source", "")}
