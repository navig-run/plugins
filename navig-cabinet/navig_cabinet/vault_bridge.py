"""Carry the navig vault inside a cabinet backup.

The vault seals every secret under a key derived from THIS machine's identity, and it
has no export of its own — so a dead disk or a reinstall loses every API key, password
and token with no way back. A cabinet backup is already the one passphrase-sealed file
that restores anywhere; this module lets it carry the vault too.

Entries are plain dicts (the payload base64'd) because they travel inside the backup's
encrypted stream — they are never written to disk in this form. A restored item keeps
its id (a credential's id IS the item id, and config/profiles refer to it) and is
re-sealed under the destination machine's own vault key.
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

FORMAT = 1


class VaultBridgeError(Exception):
    pass


@dataclass
class VaultExport:
    entries: list[dict] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)  # labels this machine cannot decrypt


@dataclass
class VaultRestoreStats:
    restored: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


def _vault(vault_dir=None):
    """The live vault — or, with *vault_dir*, a Vault over exactly that directory.

    ``get_vault()`` is a process singleton that ignores its argument after first use,
    so an explicit directory must build its own instance.
    """
    try:
        from navig_vault.core import Vault, get_vault
    except ImportError as exc:  # pragma: no cover - navig-vault is a hard dependency
        raise VaultBridgeError("navig-vault is not installed") from exc
    return Vault(vault_dir) if vault_dir is not None else get_vault()


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def export_vault(vault_dir=None) -> VaultExport:
    """Decrypt every vault item into memory, ready to be sealed into a backup."""
    from navig_vault.crypto import CryptoError

    v = _vault(vault_dir)
    out = VaultExport()
    for item in v.list():
        try:
            payload = v.get_bytes(item.label)
        except (CryptoError, KeyError, ValueError):
            out.unreadable.append(item.label)
            continue
        out.entries.append({
            "id": item.id,
            "kind": item.kind.value,
            "label": item.label,
            "provider": item.provider,
            "metadata": item.metadata or {},
            "created_at": _iso(item.created_at),
            "updated_at": _iso(item.updated_at),
            "last_used_at": _iso(item.last_used_at),
            "version": item.version,
            "payload": base64.b64encode(payload).decode("ascii"),
        })
    return out


def import_vault(entries: list[dict], vault_dir=None) -> VaultRestoreStats:
    """Add every entry whose label the vault does not already hold.

    An existing label is never overwritten: the secret on this machine may be newer
    than the backup, and a restore must not silently roll it back.
    """
    from navig_vault.crypto import CryptoEngine
    from navig_vault.types import VaultItem, VaultItemKind

    v = _vault(vault_dir)
    store = v.store()
    master_key = v._master_key()  # first-party: the key every put() seals under
    st = VaultRestoreStats()
    now = datetime.now(timezone.utc)
    for e in entries:
        label = e.get("label")
        try:
            if not label:
                raise ValueError("entry has no label")
            if store.get(label) is not None:
                st.skipped += 1
                continue
            payload = base64.b64decode(e["payload"], validate=True)
            item_id = e.get("id") or str(uuid.uuid4())
            if store.get_by_id(item_id) is not None:  # id taken by another label here
                item_id = str(uuid.uuid4())
            dek = CryptoEngine.generate_dek()
            store.upsert(VaultItem(
                id=item_id,
                kind=VaultItemKind(e.get("kind") or "secret"),
                label=label,
                provider=e.get("provider"),
                encrypted_dek=CryptoEngine.seal(master_key, dek),
                encrypted_blob=CryptoEngine.seal(dek, payload),
                metadata=e.get("metadata") or {},
                created_at=_dt(e.get("created_at")) or now,
                updated_at=_dt(e.get("updated_at")) or now,
                last_used_at=_dt(e.get("last_used_at")),
                version=int(e.get("version") or 1),
            ))
            store.audit(item_id, "restored")
            # Prove it reads back under this machine's key before counting it.
            if v.get_bytes(label) != payload:
                raise ValueError("restored secret does not read back")
            st.restored += 1
        except (KeyError, ValueError, TypeError) as exc:
            st.errors.append(f"vault item {label or '?'}: {exc}")
    return st
