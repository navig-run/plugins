"""Move vault items between machines as plain entries — the input and output of an export.

The vault seals every item under a key bound to THIS machine, so an item cannot simply be
copied to another one. An export decrypts the chosen items into memory as plain dicts; a
sealed container (``svg.py``) protects them in transit; an import re-seals each one under
the destination machine's own key.

Entries keep the exact shape navig-cabinet's backups already carry
(``navig_cabinet/vault_bridge.py``), so the two stay interchangeable. They are never
written to disk in this form.
"""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .crypto import CryptoEngine, CryptoError
from .types import VaultItem, VaultItemKind

ENTRY_FORMAT = 1


@dataclass
class ExportResult:
    entries: list[dict] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)  # labels this machine cannot decrypt
    missing: list[str] = field(default_factory=list)  # references that matched nothing


@dataclass
class ImportResult:
    restored: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # label already in this vault
    errors: list[str] = field(default_factory=list)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def select_items(vault, refs: list[str] | None) -> tuple[list[VaultItem], list[str]]:
    """Resolve *refs* (a label, an id, or a provider name) to items; ``None`` = everything.

    Returns ``(items, refs_that_matched_nothing)``. Order follows *refs*; duplicates drop.
    """
    every = vault.list()
    if refs is None:
        return every, []
    chosen: dict[str, VaultItem] = {}
    missing: list[str] = []
    for ref in refs:
        hits = [i for i in every if i.label == ref] or [i for i in every if i.id == ref] \
            or [i for i in every if (i.provider or "") == ref]
        if not hits:
            missing.append(ref)
        for i in hits:
            chosen.setdefault(i.id, i)
    return list(chosen.values()), missing


def export_entries(vault, items: list[VaultItem]) -> ExportResult:
    """Decrypt *items* into entries. An item that will not decrypt is named, never dropped silently."""
    out = ExportResult()
    for item in items:
        try:
            payload = vault.get_bytes(item.label)
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


def import_entries(vault, entries: list[dict], *, dry_run: bool = False) -> ImportResult:
    """Add every entry whose label this vault does not already hold.

    An existing label is never overwritten: the secret here may be newer than the export,
    and an import must not silently roll it back. Each restored item is read back under
    this machine's key before it counts.
    """
    store = vault.store()
    master_key = None if dry_run else vault._master_key()
    res = ImportResult()
    now = datetime.now(timezone.utc)
    for e in entries:
        label = e.get("label") if isinstance(e, dict) else None
        try:
            if not label:
                raise ValueError("entry has no label")
            if store.get(label) is not None:
                res.skipped.append(label)
                continue
            payload = base64.b64decode(e["payload"], validate=True)
            kind = VaultItemKind(e.get("kind") or "secret")
            if dry_run:
                res.restored.append(label)
                continue
            item_id = e.get("id") or str(uuid.uuid4())
            if store.get_by_id(item_id) is not None:  # id taken by another label here
                item_id = str(uuid.uuid4())
            dek = CryptoEngine.generate_dek()
            store.upsert(VaultItem(
                id=item_id,
                kind=kind,
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
            store.audit(item_id, "imported")
            if vault.get_bytes(label) != payload:
                raise ValueError("imported secret does not read back")
            res.restored.append(label)
        except (KeyError, ValueError, TypeError, CryptoError) as exc:
            res.errors.append(f"{label or '?'}: {exc}")
    return res


def describe(entry: dict) -> list[tuple[str, str]]:
    """``(field, value)`` pairs for showing an entry's secret — a credential's fields, or the text.

    Binary payloads are summarised, never dumped to a terminal.
    """
    try:
        raw = base64.b64decode(entry.get("payload") or "", validate=True)
    except ValueError:
        return [("secret", "<unreadable payload>")]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return [("file", f"<binary, {len(raw)} bytes>")]
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return [("secret", text)]
    if isinstance(doc, dict):
        data = doc.get("data") if isinstance(doc.get("data"), dict) else doc
        return [(str(k), v if isinstance(v, str) else json.dumps(v)) for k, v in data.items()]
    return [("secret", text)]
