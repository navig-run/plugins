"""NAVIG Vault — unified high-level API (AES-GCM + Argon2id per-item DEK).

This is the **single canonical vault** for all NAVIG credential storage.
The legacy ``CredentialsVault`` (core.py) is now a thin redirect shim to
this class; all historical call sites should converge here.

Usage
-----
from .core import get_vault

v = get_vault()
v.unlock()                          # machine-fingerprint unlock (no passphrase)
v.unlock(passphrase=b"mypass")      # passphrase unlock

item_id = v.put("openai/api_key", b"sk-...")
secret  = v.get_secret("openai/api_key")  # → "sk-..."

# Provider/profile compatibility API still works:
cred_id = v.add("openai", "api_key", {"api_key": "sk-..."}, profile_id="default")
cred    = v.get("openai")           # → Credential | None
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import threading
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from ._compat import set_owner_only_file_permissions
from ._compat import atomic_write_text

from .crypto import CryptoEngine, CryptoError
from .keyslots import FINGERPRINT, PASSPHRASE, RECOVERY, Keyring, VaultLocked, forget_cached_vmks
from .secret_str import SecretStr
from .session import SessionStore, VaultSession
from .store import VaultConflict, VaultStore
from .types import (
    Credential,
    CredentialInfo,
    CredentialType,
    TestResult,
    VaultItem,
    VaultItemKind,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    pass

__all__ = ["Vault", "VaultConflict", "VaultLocked", "acting_as", "get_vault"]

from ._constants import _DEFAULT_TTL  # noqa: E402 — leaf module, no circular risk

# Who the vault records in its audit log for the current context. ``user`` by
# default; the federation authority sets ``device:<id>`` around a device request,
# so a read or a denial names the machine that made it.
_ACTOR: contextvars.ContextVar[str] = contextvars.ContextVar("navig_vault_actor", default="user")


@contextlib.contextmanager
def acting_as(actor: str) -> Iterator[None]:
    """Attribute every vault audit entry inside the block to *actor*."""
    token = _ACTOR.set(actor)
    try:
        yield
    finally:
        _ACTOR.reset(token)

# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────


def _cred_label(provider: str, profile_id: str | None) -> str:
    """Return the canonical vault label for a provider/profile credential."""
    profile = (profile_id or "default").strip()
    if profile == "default":
        return provider.lower().strip()
    return f"{provider.lower().strip()}/{profile}"


def _decode_payload(payload: bytes) -> dict:
    """Decode a DECRYPTED vault payload into a credential's data dict.

    Payloads are JSON objects today, but the vault predates that convention: the
    oldest items store the secret as a bare UTF-8 string (an API key, a session
    blob, a hash). Those decrypt perfectly — only ``json.loads`` refuses them.

    Treating that refusal as "empty" is the SAME phantom-empty-credential bug the
    unreadable branch guards against, arriving through the other door: the secret
    is right there, readable, and would be thrown away, so ``get_api_key`` reports
    the provider as unconfigured while ``list`` still shows it connected. Worse,
    a read-then-write caller would persist the ``{}`` over a live secret.

    So a non-JSON payload is wrapped as ``{"value": <text>}`` — ``value`` is a
    field both ``get_api_key`` and ``get_secret`` already resolve. An empty payload
    is genuinely empty and decodes to ``{}``.

    Bytes that are not valid UTF-8 at all are NOT swallowed: ``json.loads`` raises
    ``UnicodeDecodeError`` and it propagates, so the caller reports the credential
    UNREADABLE rather than inventing an empty one. Binary belongs to FILE/CERT
    items, which never come through here.
    """
    if not payload:
        return {}
    try:
        decoded = json.loads(payload)  # UnicodeDecodeError on binary → propagates
    except json.JSONDecodeError:
        # Valid UTF-8 (json.loads got far enough to reject it as JSON), just not
        # JSON — i.e. the pre-JSON bare-secret shape.
        text = payload.decode("utf-8").strip()
        return {"value": text} if text else {}
    # A JSON scalar ("abc", 42) is legacy too — same wrapping, not a dict cast.
    if isinstance(decoded, dict):
        return decoded
    return {"value": decoded} if decoded not in (None, "") else {}


def _item_to_credential(item: VaultItem, data: dict | None = None) -> Credential:
    """Reconstruct a ``Credential`` from a ``VaultItem`` and its decrypted data."""
    meta = item.metadata or {}
    ct_raw = meta.get("credential_type", "generic")
    try:
        cred_type = CredentialType(ct_raw)
    except ValueError:
        cred_type = CredentialType.GENERIC

    return Credential(
        id=item.id,
        provider=item.provider or item.label.split("/")[0],
        profile_id=meta.get("profile_id", "default"),
        credential_type=cred_type,
        label=meta.get("label", item.label),
        data=data or {},
        metadata={
            k: v
            for k, v in meta.items()
            if k not in ("credential_type", "profile_id", "label", "enabled")
        },
        enabled=bool(meta.get("enabled", True)),
        created_at=item.created_at,
        updated_at=item.updated_at,
        last_used_at=item.last_used_at,
    )


def _item_to_cred_info(item: VaultItem) -> CredentialInfo:
    """Return a metadata-only ``CredentialInfo`` from a ``VaultItem``."""
    meta = item.metadata or {}
    ct_raw = meta.get("credential_type", "generic")
    try:
        cred_type = CredentialType(ct_raw)
    except ValueError:
        cred_type = CredentialType.GENERIC
    return CredentialInfo(
        id=item.id,
        provider=item.provider or item.label.split("/")[0],
        profile_id=meta.get("profile_id", "default"),
        credential_type=cred_type,
        label=meta.get("label", item.label),
        enabled=bool(meta.get("enabled", True)),
        created_at=item.created_at,
        last_used_at=item.last_used_at,
        metadata={
            k: v
            for k, v in meta.items()
            if k not in ("credential_type", "profile_id", "label", "enabled")
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# Vault
# ─────────────────────────────────────────────────────────────────────────────


class Vault:
    """Unified vault — AES-GCM + Argon2id per-item DEK, with compatibility adapter API.

    Parameters
    ----------
    vault_dir : Path
        Directory for vault.db and vault.salt.
    """

    def __init__(
        self,
        vault_dir: Path | None = None,
        *,
        vault_path: Path | None = None,
        auto_migrate: bool = False,  # accepted for compat; migration is handled by get_vault()
    ) -> None:
        # Accept vault_path as backward-compat alias for vault_dir (old CredentialsVault API)
        if vault_dir is None and vault_path is not None:
            # vault_path may be the .db file itself; we need the parent directory
            _vp = Path(vault_path)
            vault_dir = _vp.parent if _vp.suffix == ".db" else _vp
        if vault_dir is None:
            from ._compat import vault_dir as _vault_dir_fn  # noqa: PLC0415

            vault_dir = _vault_dir_fn()
        self.vault_dir = vault_dir
        # Store original vault_path kwarg for backward compat (callers may read .vault_path)
        self._vault_path_arg: Path | None = Path(vault_path) if vault_path is not None else None
        self._engine = CryptoEngine(vault_dir)
        self._store = VaultStore(vault_dir)
        self._keyring = Keyring(self._store, self._engine, vault_dir)
        self._init_lock = threading.Lock()
        if auto_migrate:
            _migrate_auth_profiles(self)

    # ── Keyslots ──────────────────────────────────────────────────────────────

    @property
    def keyring(self) -> Keyring:
        return self._keyring

    def is_legacy(self) -> bool:
        """True while the vault has no keyslots (master key = KDF(fingerprint/passphrase))."""
        return not self._keyring.initialized()

    def _ensure_initialized(self) -> None:
        """Give a brand-new, EMPTY vault a random VMK and its default keyslots.

        A vault that already holds items is never touched here: it stays legacy
        until `migrate_to_keyslots` is run on purpose (it has to keep opening with
        the key its items were sealed under).
        """
        if self._keyring.initialized():
            return
        with self._init_lock:
            if self._keyring.initialized() or self._store.count() > 0:
                return
            self._keyring.initialize(os.urandom(32))

    def migrate_to_keyslots(self) -> list[str]:
        """Turn a legacy vault into a keyslot vault, without touching one item.

        The current master key BECOMES the VMK (so no DEK is re-wrapped and older
        navig keeps reading the vault), then the device + fingerprint slots and the
        canary are written in one transaction. Proven first: the key must open a
        stored DEK. Idempotent — a second run is a no-op. Returns the slot kinds made.
        """
        if self._keyring.initialized():
            return []
        master = self._legacy_master_key()
        sample = next((i for i in self._store.list() if i.encrypted_dek), None)
        if sample is not None:
            CryptoEngine.open(master, sample.encrypted_dek)  # CryptoError → nothing written
        return [s.kind for s in self._keyring.initialize(master)]

    def add_passphrase(self, passphrase: bytes) -> None:
        """Add (or replace) the passphrase keyslot."""
        self._ensure_initialized()
        vmk = self._master_key()
        self._keyring.add(self._keyring.wrap(PASSPHRASE, vmk, passphrase=passphrase), vmk, replace_kind=True)

    def add_recovery(self) -> str:
        """Create a recovery keyslot; returns the recovery key — shown ONCE, never stored.

        Replaces any earlier recovery slot: the previous key stops working.
        """
        self._ensure_initialized()
        vmk = self._master_key()
        private_raw, public_raw = CryptoEngine.x25519_generate()
        self._keyring.add(
            self._keyring.wrap(RECOVERY, vmk, recipient_public=public_raw), vmk, replace_kind=True
        )
        from .keyslots import format_recovery_key  # noqa: PLC0415

        return format_recovery_key(private_raw)

    def verify_recovery(self, recovery_key: str) -> bool:
        """True when *recovery_key* opens this vault's recovery slot."""
        from .keyslots import parse_recovery_key  # noqa: PLC0415

        try:
            self._keyring.unlock_with(RECOVERY, parse_recovery_key(recovery_key))
            return True
        except (VaultLocked, ValueError):
            return False

    # ── Unlock / Lock ─────────────────────────────────────────────────────────

    @property
    def vault_path(self) -> Path:
        """Return the vault .db file path (backward-compat with CredentialsVault API)."""
        if self._vault_path_arg is not None:
            return self._vault_path_arg
        return self.vault_dir / "vault.db"

    def unlock(
        self,
        passphrase: bytes | None = None,
        ttl_seconds: int = _DEFAULT_TTL,
    ) -> VaultSession:
        """Unlock into a session.

        Keyslot vault: a passphrase opens the passphrase slot (``VaultLocked`` when
        there is none or it is wrong); no passphrase opens an automatic slot. A
        brand-new empty vault unlocked WITH a passphrase gets a passphrase slot.
        Legacy vault: the key is derived as it always was.
        """
        if passphrase is not None and not self._keyring.initialized() and self._store.count() == 0:
            self._ensure_initialized()
            self.add_passphrase(passphrase)
        if self._keyring.initialized():
            master_key = (
                self._keyring.unlock_with(PASSPHRASE, passphrase)
                if passphrase is not None
                else self._keyring.unlock_auto()
            )
        else:
            master_key = self._engine.derive_key(passphrase)
        session = VaultSession(
            master_key=master_key,
            unlocked_at=datetime.now(timezone.utc),
            ttl_seconds=ttl_seconds,
        )
        SessionStore.set(session)
        return session

    def lock(self) -> None:
        """Clear active session (zero out key from memory)."""
        SessionStore.clear()
        forget_cached_vmks()

    def _legacy_master_key(self) -> bytes:
        session = SessionStore.get()
        if session is not None:
            return session.master_key
        if SessionStore.expired():
            # The passphrase session timed out. Falling back to the fingerprint key
            # here used to make reads fail and — worse — seal NEW items under a
            # different key than the rest of the vault.
            raise VaultLocked("the vault passphrase session expired — unlock again")
        return self._engine.derive_key(None)

    def _master_key(self) -> bytes:
        """The key every DEK is sealed under: the VMK, or a legacy vault's derived key."""
        if not self._keyring.initialized():
            return self._legacy_master_key()
        session = SessionStore.get()
        # The session store is process-wide, not per vault: only trust its key if it
        # is THIS vault's VMK (one AES-GCM check), never seal under another vault's.
        if session is not None and self._keyring._check(session.master_key):
            return session.master_key
        return self._keyring.unlock_auto()

    # ── Core operations ───────────────────────────────────────────────────────

    def put(
        self,
        label: str,
        payload: bytes,
        *,
        kind: VaultItemKind = VaultItemKind.SECRET,
        provider: str | None = None,
        metadata: dict | None = None,
        expected_version: int | None = None,
    ) -> str:
        """Encrypt and store *payload* under *label*.  Returns the item UUID.

        ``expected_version`` makes the write conditional (compare-and-swap): ``0``
        = must not exist, ``N`` = must still be at version N; otherwise
        :class:`VaultConflict` and nothing is written. The replaced version is kept
        sealed in the item history either way.
        """
        self._ensure_initialized()
        master_key = self._master_key()

        dek = CryptoEngine.generate_dek()
        encrypted_blob = CryptoEngine.seal(dek, payload)
        encrypted_dek = CryptoEngine.seal(master_key, dek)

        item_id, _version, _created = self._store.put_item(
            new_id=str(uuid.uuid4()),
            label=label,
            kind=kind,
            provider=provider,
            encrypted_dek=encrypted_dek,
            encrypted_blob=encrypted_blob,
            metadata=metadata or {},
            expected_version=expected_version,
            actor=_ACTOR.get(),
        )
        return item_id

    def unreadable_items(self) -> list[str]:
        """Labels whose DEK does not open under this vault's key (no payload is decrypted).

        Such an item was sealed under another key long ago; nothing that holds this
        vault's key can read it. Reported by status/backup/rotate instead of failing
        them one confusing error at a time.
        """
        key = self._master_key()
        out = []
        for item in self._store.list():
            if not item.encrypted_dek:
                continue
            try:
                CryptoEngine.open(key, item.encrypted_dek)
            except CryptoError:
                out.append(item.label)
        return out

    def version_of(self, label: str) -> int:
        """The stored version of *label*, ``0`` when absent (for ``expected_version``)."""
        item = self._store.get(label)
        return item.version if item else 0

    def get_bytes(self, label: str, *, version: int | None = None) -> bytes:
        """Decrypt and return raw payload bytes for *label*.

        ``version`` reads that version — the live one, or a replaced one from the
        item history.

        Raises
        ------
        KeyError    : Item (or that version) not found.
        CryptoError : Decryption failure.
        """
        item = self._store.get(label)
        if item is None:
            raise KeyError(f"Vault item not found: {label!r}")
        encrypted_dek, encrypted_blob = item.encrypted_dek, item.encrypted_blob
        if version is not None and version != item.version:
            with self._store._lock:
                row = self._store._connect().execute(
                    "SELECT encrypted_dek, encrypted_blob FROM vault_item_history "
                    "WHERE item_id = ? AND version = ?",
                    (item.id, version),
                ).fetchone()
            if row is None:
                raise KeyError(f"Vault item {label!r} has no version {version}")
            encrypted_dek, encrypted_blob = bytes(row[0]), bytes(row[1])
        master_key = self._master_key()
        dek = CryptoEngine.open(master_key, encrypted_dek)
        payload = CryptoEngine.open(dek, encrypted_blob)
        self._store.audit(item.id, "accessed", actor=_ACTOR.get())
        return payload

    def _payload_readable(self, label: str) -> bool:
        """True when *label*'s secret actually decrypts.

        Used only to break ties between several credentials for one provider — a
        corrupt one must not shadow a working one. Deliberately swallows every
        error: this is a probe, not an accessor.
        """
        try:
            self.get_bytes(label)
            return True
        except Exception:  # noqa: BLE001 — probe: unreadable is the answer, not a crash
            return False

    def get_secret(self, label: str) -> "SecretStr":
        """Decrypt and return the primary secret string for *label* wrapped in SecretStr.

        For SECRET/PROVIDER items: "value", then "api_key", then first value.
        For NOTE items: "text" field.
        For FILE/CERT/other items: raw bytes decoded as UTF-8.

        Raises
        ------
        KeyError : Item not found.
        """
        item = self._store.get(label)
        if item is None:
            raise KeyError(f"Vault item not found: {label!r}")

        payload = self.get_bytes(label)

        if item.kind in (VaultItemKind.SECRET, VaultItemKind.PROVIDER, VaultItemKind.CREDENTIAL):
            # _decode_payload, not a bare json.loads: the oldest items store the
            # secret as a plain string, which json.loads REFUSES. That threw out of
            # here and — because get_api_key wraps this call in a blanket
            # `except Exception: pass` — took the working cred fallback down with
            # it, so a perfectly readable legacy key resolved to None. The NOTE
            # branch below has always tolerated raw text; this one now does too.
            data = _decode_payload(payload)
            if "value" in data:
                return SecretStr(data["value"])
            if item.provider:
                from .provider import get_provider  # noqa: PLC0415

                meta = get_provider(item.provider) or {}
                key_field = meta.get("key_field", "api_key")
                if key_field in data:
                    return SecretStr(data[key_field])
            if "api_key" in data:
                return SecretStr(data["api_key"])
            if "token" in data:
                return SecretStr(data["token"])
            first = next(iter(data.values()), "")
            return SecretStr(str(first) if first is not None else "")
        elif item.kind == VaultItemKind.NOTE:
            try:
                data = json.loads(payload)
                return SecretStr(data.get("text", payload.decode("utf-8", errors="replace")))
            except (json.JSONDecodeError, ValueError):
                return SecretStr(payload.decode("utf-8", errors="replace"))
        else:
            return SecretStr(payload.decode("utf-8", errors="replace"))

    def batch_get(self, keys: list[str]) -> dict[str, str]:
        """Fetch multiple vault secrets by label, silently skipping missing keys.

        Args:
            keys: Vault item labels (or parameter names that map 1-to-1 to
                  vault labels) to look up.

        Returns:
            Mapping of ``{key: plaintext_value}`` for every key found.
            Keys that are not stored in the vault are omitted rather than
            raising an exception — callers that need a strict check should
            call :meth:`get_secret` directly.
        """
        result: dict[str, str] = {}
        for key in keys:
            try:
                result[key] = self.get_secret(key).reveal()
            except (KeyError, Exception):
                pass
        return result

    def delete(self, label_or_id: str) -> bool:
        """Delete item by label or UUID.  Returns True if deleted.

        If *label_or_id* looks like a UUID (8-char hex prefix or full UUID),
        the item is looked up by ID first so that callers using credential
        IDs work without knowing the label.
        """
        # Try direct label delete first (most common for label-based callers).
        # The store audits the delete in the same transaction.
        if self._store.delete(label_or_id, actor=_ACTOR.get()):
            return True
        # Fall back: look up by ID (legacy callers may pass IDs)
        item = self._store.get_by_id(label_or_id)
        if item is not None:
            return self._store.delete(item.label, actor=_ACTOR.get())
        return False

    # ── Rotate master key ─────────────────────────────────────────────────────

    def rotate(self, new_passphrase: bytes | None = None) -> int:
        """Replace the VMK with a fresh random one. Returns the number of items re-wrapped.

        All or nothing: every item DEK, every replaced-version DEK in the history,
        every keyslot and the canary are re-wrapped inside ONE transaction. A DEK
        that does not open under the current key aborts the whole rotation with
        :class:`CryptoError` and leaves the vault exactly as it was — the old code
        wrote a new salt first and then SKIPPED items it could not open, so a
        crash or one bad item left a vault split across two keys.

        A legacy vault is migrated to keyslots first. ``new_passphrase`` re-wraps
        (or creates) the passphrase slot; if the vault has a passphrase slot it is
        required, so rotation never silently drops a way in.
        """
        if new_passphrase is None and any(s.kind == PASSPHRASE for s in self._keyring.slots()):
            raise ValueError("this vault has a passphrase slot — pass the passphrase to rotate")
        if self.is_legacy():
            if self._store.count() == 0:
                self._ensure_initialized()
            else:
                self.migrate_to_keyslots()
        old_master = self._master_key()
        unreadable = self.unreadable_items()
        if unreadable:
            # Checked up front so the refusal names them; the transaction below would
            # roll back on the first one anyway, with a bare "decryption failed".
            raise CryptoError(
                f"{len(unreadable)} item(s) do not open under this vault's key and would be "
                f"lost by a rotation: {', '.join(unreadable)} — delete them (navig vault "
                "delete <label>) or restore them first"
            )
        new_master = os.urandom(32)
        now = datetime.now(timezone.utc).isoformat()
        actor = _ACTOR.get()
        rotated = 0
        with self._store.transaction() as conn:
            for row in conn.execute("SELECT id, encrypted_dek FROM vault_items").fetchall():
                dek = CryptoEngine.open(old_master, row["encrypted_dek"])  # CryptoError → ROLLBACK
                conn.execute(
                    "UPDATE vault_items SET encrypted_dek = ? WHERE id = ?",
                    (CryptoEngine.seal(new_master, dek), row["id"]),
                )
                conn.execute(
                    "INSERT INTO vault_audit (item_id, action, actor, ts, detail) VALUES (?,?,?,?,?)",
                    (row["id"], "rotate", actor, now, ""),
                )
                rotated += 1
            for row in conn.execute(
                "SELECT item_id, version, encrypted_dek FROM vault_item_history"
            ).fetchall():
                dek = CryptoEngine.open(old_master, row["encrypted_dek"])
                conn.execute(
                    "UPDATE vault_item_history SET encrypted_dek = ? WHERE item_id = ? AND version = ?",
                    (CryptoEngine.seal(new_master, dek), row["item_id"], row["version"]),
                )
            had_passphrase = any(s.kind == PASSPHRASE for s in self._keyring.slots())
            self._keyring.rewrap_all(conn, new_master, passphrase=new_passphrase)
            if new_passphrase is not None and not had_passphrase:
                self._keyring._insert(
                    conn, self._keyring.wrap(PASSPHRASE, new_master, passphrase=new_passphrase)
                )
        forget_cached_vmks()
        session = SessionStore.get()
        if session is not None:
            SessionStore.set(
                VaultSession(
                    master_key=new_master,
                    unlocked_at=session.unlocked_at,
                    ttl_seconds=session.ttl_seconds,
                )
            )
        return rotated

    # ── Fingerprint-change detection ──────────────────────────────────────────

    def _fingerprint_path(self) -> Path:
        """Path to the file that caches the current machine fingerprint."""
        return self.vault_dir / ".vault_fp"

    def _read_stored_fingerprint(self) -> bytes | None:
        """Return the fingerprint stored on disk, or ``None`` if absent."""
        fp = self._fingerprint_path()
        try:
            data = fp.read_bytes()
            return data if data else None
        except OSError:
            return None

    def _write_stored_fingerprint(self, fp_bytes: bytes) -> None:
        """Persist the fingerprint to disk (best-effort)."""
        fp = self._fingerprint_path()
        try:
            fp.write_bytes(fp_bytes)
            set_owner_only_file_permissions(fp)
        except Exception:
            pass

    def _rekey_from_old_fingerprint(self, old_fp_bytes: bytes) -> int:
        """Re-wrap all DEK envelopes: old fingerprint-derived key → current key.

        This is the vault-side analogue of ``rotate()`` but instead of
        generating a new salt it keeps the existing salt and simply swaps the
        key-encryption key.  Returns the number of items successfully re-keyed.
        """
        salt = self._engine._get_or_create_salt()
        old_master = self._engine._kdf(old_fp_bytes, salt)
        new_master = self._master_key()
        items = self._store.list()
        rekeyed = 0
        for item in items:
            if not item.encrypted_dek or not item.encrypted_blob:
                continue
            try:
                dek = CryptoEngine.open(old_master, item.encrypted_dek)
                new_enc_dek = CryptoEngine.seal(new_master, dek)
                rotated_item = VaultItem(
                    id=item.id,
                    kind=item.kind,
                    label=item.label,
                    provider=item.provider,
                    encrypted_dek=new_enc_dek,
                    encrypted_blob=item.encrypted_blob,
                    metadata=item.metadata,
                    created_at=item.created_at,
                    updated_at=datetime.now(timezone.utc),
                    last_used_at=item.last_used_at,
                    version=item.version + 1,
                )
                self._store.upsert(rotated_item)
                rekeyed += 1
            except CryptoError:
                pass
        return rekeyed

    # ── JSON key files ────────────────────────────────────────────────────────

    def put_json_file(
        self,
        label: str,
        source: str | Path,
        *,
        provider: str | None = None,
        metadata: dict | None = None,
    ) -> str:
        """Encrypt and store a JSON key file (e.g. Google service account)."""
        import json as _json  # noqa: PLC0415

        src_path = Path(source).expanduser() if isinstance(source, (str, Path)) else None
        if src_path is not None and src_path.exists():
            raw = src_path.read_text(encoding="utf-8")
            original_name = src_path.name
        else:
            raw = str(source)
            original_name = None

        try:
            _json.loads(raw)
        except _json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON provided to put_json_file: {exc}") from exc

        meta = dict(metadata or {})
        meta["content_type"] = "application/json"
        if original_name:
            meta["original_file"] = original_name

        return self.put(
            label, raw.encode("utf-8"), kind=VaultItemKind.FILE, provider=provider, metadata=meta
        )

    def get_json_file(self, label: str) -> dict:
        """Decrypt and return a stored JSON key file as a Python dict."""
        import json as _json  # noqa: PLC0415

        return _json.loads(self.get_bytes(label))

    def get_json_str(self, label: str) -> str:
        """Decrypt and return a stored JSON key file as a raw JSON string."""
        return self.get_bytes(label).decode("utf-8")

    # ── Native label-based list / search / count ─────────────────────────────

    def list(
        self,
        kind: VaultItemKind | None = None,
        provider: str | None = None,
        profile_id: str | None = None,
    ) -> list[VaultItem]:
        """Return vault items, optionally filtered.

        When *profile_id* is supplied the results are further narrowed by
        ``item.metadata["profile_id"]``.
        """
        items = self._store.list(kind=kind, provider=provider)
        if profile_id is not None:
            # Check the direct attribute first (set when CredentialInfo is constructed
            # from stored metadata), then fall back to the metadata dict for items
            # that stored profile_id only there.
            items = [
                i
                for i in items
                if getattr(i, "profile_id", None) == profile_id
                or i.metadata.get("profile_id", "default") == profile_id
            ]
        return items

    def search(self, query: str) -> list[VaultItem]:
        return self._store.search(query)

    def count(self, provider: str | None = None) -> int:
        return self._store.count(provider=provider) if provider else self._store.count()

    def store(self) -> VaultStore:
        """Expose the underlying store (for advanced use)."""
        return self._store

    def engine(self) -> CryptoEngine:
        """Expose the crypto engine (for advanced use)."""
        return self._engine

    # ─────────────────────────────────────────────────────────────────────────
    # Provider/profile compatibility adapter API
    # These methods bridge the old CredentialsVault (provider/profile model)
    # to the new label-based storage.  Zero changes required in call sites.
    # ─────────────────────────────────────────────────────────────────────────

    def add(
        self,
        provider: str,
        credential_type: str = "generic",
        data: dict | None = None,
        profile_id: str = "default",
        label: str | None = None,
        metadata: dict | None = None,
    ) -> str:
        """Store a provider/profile credential and return its ID.

        Compatible with ``CredentialsVault.add()``.
        """
        v_label = _cred_label(provider, profile_id)
        display_label = label or f"{provider.title()} ({profile_id})"
        meta = dict(metadata or {})
        meta.update(
            {
                "credential_type": credential_type,
                "profile_id": profile_id,
                "label": display_label,
                "enabled": meta.get("enabled", True),
            }
        )
        return self.put(
            v_label,
            json.dumps(data or {}).encode(),
            kind=VaultItemKind.PROVIDER,
            provider=provider,
            metadata=meta,
        )

    def get(
        self,
        provider: str,
        profile_id: str | None = None,
        caller: str = "unknown",
    ) -> Credential | None:
        """Return a ``Credential`` for *provider* / *profile_id*, or None if not found."""
        v_label = _cred_label(provider, profile_id or "default")
        item = self._store.get(v_label)
        if item is None:
            # Fallback: search by provider tag (picks first match)
            matches = self._store.list(provider=provider)
            prof = profile_id or "default"
            # Check direct attribute first, then metadata dict (credentials added
            # via the Telegram /provider wizard may not store profile_id in metadata)
            profile_matches = [
                m
                for m in matches
                if getattr(m, "profile_id", None) == prof
                or m.metadata.get("profile_id", "default") == prof
            ]
            if not profile_matches and prof != "default" and matches:
                # Cross-profile fallback: if the requested profile has nothing,
                # accept credentials from the default profile.  This handles the
                # common case where keys were stored before the active profile was
                # switched (e.g. stored in 'default', active profile is 'work').
                profile_matches = [
                    m
                    for m in matches
                    if getattr(m, "profile_id", None) == "default"
                    or m.metadata.get("profile_id", "default") == "default"
                ]
            if not profile_matches:
                return None
            # Priority: active=True → most-recently-used → created_at desc.
            active_m = [m for m in profile_matches if m.metadata.get("active", False)]
            ranked = active_m or sorted(
                profile_matches,
                key=lambda m: m.last_used_at or m.created_at,
                reverse=True,
            )
            # …but a candidate whose secret cannot be DECRYPTED must never shadow a
            # readable sibling for the same provider. This used to take ranked[0]
            # unconditionally, which made a corrupt credential permanently sticky:
            # get_bytes() audits the access BEFORE decryption fails, so every failed
            # read bumps that item's last_used_at, re-electing it as "most recent"
            # next time. One dead credential therefore hid a perfectly good key for
            # the same provider forever, and the failure looked intermittent
            # (label-direct hits worked, provider lookups did not).
            item = next(
                (m for m in ranked if self._payload_readable(m.label)),
                ranked[0],  # nothing readable → keep the old behaviour and report it
            )
        # Return None for disabled credentials
        if not item.metadata.get("enabled", True):
            return None
        try:
            payload = self.get_bytes(item.label)
            decoded_data = _decode_payload(payload)
        except KeyError:
            decoded_data = {}  # genuinely missing payload — empty is honest
        except Exception:  # noqa: BLE001 — CryptoError / OSError / sqlite lock → UNREADABLE, not empty
            # Do NOT return a phantom empty-but-ENABLED credential: that reports a live
            # secret as absent (get_api_key falls through to env var / None) while
            # list_creds still shows the provider connected, and any read-then-write caller
            # would persist the {} over the real secret. Mirror update()'s #489 split —
            # unreadable ≠ empty.
            logger.warning(
                "vault.get(%s): secret is unreadable; returning None instead of a "
                "phantom empty credential", item.label)
            return None
        return _item_to_credential(item, decoded_data)

    def get_api_key(
        self,
        provider: str,
        profile_id: str | None = None,
        caller: str = "unknown",
    ) -> str | None:
        """Return the primary API key for *provider*, or None if not stored.

        Resolution order:
        1. Vault lookup (active profile when profile_id is None)
        2. Environment variable: ``{PROVIDER_UPPER}_API_KEY``
        """
        import os as _os  # noqa: PLC0415

        resolved_profile = profile_id or self.get_active_profile()
        try:
            v_label = _cred_label(provider, resolved_profile)
            # Try label-direct first
            try:
                secret = self.get_secret(v_label)
                if secret is not None:
                    return secret.reveal() if hasattr(secret, "reveal") else str(secret)
            except KeyError:
                pass
            # Compatibility fallback – also tries active profile
            cred = self.get(provider, resolved_profile, caller=caller)
            if cred is not None:
                val = cred.data.get("api_key") or cred.data.get("token") or cred.data.get("value")
                if val:
                    return str(val)
        except Exception:  # noqa: BLE001
            pass

        # Environment variable fallback: PROVIDER_API_KEY (e.g. GROQ_API_KEY)
        env_key = provider.upper().replace(".", "_").replace("-", "_") + "_API_KEY"
        env_val = _os.environ.get(env_key)
        if env_val:
            return env_val

        return None

    def get_by_id(
        self,
        credential_id: str,
        caller: str = "unknown",
    ) -> Credential | None:
        """Return a full ``Credential`` by its UUID, or None if not found."""
        item = self._store.get_by_id(credential_id)
        if item is None:
            return None
        try:
            payload = self.get_bytes(item.label)
            decoded_data = _decode_payload(payload)
        except KeyError:
            decoded_data = {}
        except Exception:  # noqa: BLE001 — unreadable (CryptoError/OSError/lock) ≠ empty (see get())
            logger.warning(
                "vault.get_by_id(%s): secret is unreadable; returning None instead of a "
                "phantom empty credential", item.label)
            return None
        return _item_to_credential(item, decoded_data)

    def update(
        self,
        credential_id: str,
        data: dict | None = None,
        label: str | None = None,
        metadata: dict | None = None,
    ) -> bool:
        """Update credential data and/or label.  Returns True if found and updated."""
        item = self._store.get_by_id(credential_id)
        if item is None:
            return False

        # Decrypt existing data and merge
        decrypt_failed = False
        try:
            existing_payload = self.get_bytes(item.label)
            existing_data = json.loads(existing_payload)
        except (KeyError, json.JSONDecodeError):
            existing_data = {}
        except Exception:  # noqa: BLE001  — e.g. CryptoError, OSError
            # Decryption failed.  If the caller is only updating metadata/label
            # (data=None), preserve the encrypted blob unchanged to prevent
            # silently wiping the credential's secret.
            decrypt_failed = True
            existing_data = {}

        if decrypt_failed and data is not None:
            # A data update was requested, but the existing secret could NOT be read
            # (the store is SQLite/WAL — a transient sharing-violation lock raises here,
            # as does a genuine crypto failure). `existing_data` is an empty {}, so
            # merging `data` into it and writing back would silently DROP every OTHER
            # field of the credential's secret. Refuse rather than partial-wipe a
            # possibly-live secret — the caller can retry once the store reads cleanly,
            # or delete+recreate if it is truly corrupt. (Mirrors sigil_store / the whole
            # "never overwrite what you couldn't read" class.)
            logger.warning(
                "vault.update(%s): existing secret is unreadable; refusing a partial "
                "write that would drop the credential's other fields",
                credential_id,
            )
            return False

        if decrypt_failed and data is None:
            # Metadata / label-only update — patch the item metadata in-place
            # without touching the encrypted DEK or blob.
            new_meta = dict(item.metadata)
            if metadata is not None:
                new_meta.update(metadata)
            if label is not None:
                new_meta["label"] = label
            patched = VaultItem(
                id=item.id,
                kind=item.kind,
                label=item.label,
                provider=item.provider,
                encrypted_dek=item.encrypted_dek,
                encrypted_blob=item.encrypted_blob,
                metadata=new_meta,
                created_at=item.created_at,
                updated_at=datetime.now(timezone.utc),
                last_used_at=item.last_used_at,
                version=item.version + 1,
            )
            self._store.upsert(patched)
            return True

        if data is not None:
            existing_data.update(data)

        new_meta = dict(item.metadata)
        if metadata is not None:
            new_meta.update(metadata)
        if label is not None:
            new_meta["label"] = label

        self.put(
            item.label,
            json.dumps(existing_data).encode(),
            kind=item.kind,
            provider=item.provider,
            metadata=new_meta,
        )
        return True

    def activate(self, credential_id: str) -> bool:
        """Mark a credential as the active (preferred) one for its provider.

        Clears the ``active`` flag from every other credential for the same
        provider, then sets it on this one. Also syncs the active-profile file
        so ``get_api_key()`` resolves it automatically.

        Returns True if found and updated.
        """
        target = self._store.get_by_id(credential_id)
        if target is None:
            return False

        # Clear active flag on all siblings (same provider, different id)
        for sib in self._store.list(provider=target.provider):
            if sib.id == target.id:
                continue
            if sib.metadata.get("active", False):
                sib_meta = dict(sib.metadata)
                sib_meta["active"] = False
                self._store.upsert(
                    VaultItem(
                        id=sib.id,
                        kind=sib.kind,
                        label=sib.label,
                        provider=sib.provider,
                        encrypted_dek=sib.encrypted_dek,
                        encrypted_blob=sib.encrypted_blob,
                        metadata=sib_meta,
                        created_at=sib.created_at,
                        updated_at=datetime.now(timezone.utc),
                        last_used_at=sib.last_used_at,
                        version=sib.version + 1,
                    )
                )

        # Set active flag on target
        tgt_meta = dict(target.metadata)
        tgt_meta["active"] = True
        self._store.upsert(
            VaultItem(
                id=target.id,
                kind=target.kind,
                label=target.label,
                provider=target.provider,
                encrypted_dek=target.encrypted_dek,
                encrypted_blob=target.encrypted_blob,
                metadata=tgt_meta,
                created_at=target.created_at,
                updated_at=datetime.now(timezone.utc),
                last_used_at=target.last_used_at,
                version=target.version + 1,
            )
        )
        self._store.audit(target.id, "activated")
        self.set_active_profile(target.metadata.get("profile_id", "default"))
        return True

    def enable(self, credential_id: str) -> bool:
        """Mark a credential as enabled.  Returns True if found."""
        item = self._store.get_by_id(credential_id)
        if item is None:
            return False
        new_meta = dict(item.metadata)
        new_meta["enabled"] = True
        # Re-put preserving encrypted blob (no-decrypt shortcut via upsert)
        updated = VaultItem(
            id=item.id,
            kind=item.kind,
            label=item.label,
            provider=item.provider,
            encrypted_dek=item.encrypted_dek,
            encrypted_blob=item.encrypted_blob,
            metadata=new_meta,
            created_at=item.created_at,
            updated_at=datetime.now(timezone.utc),
            last_used_at=item.last_used_at,
            version=item.version + 1,
        )
        self._store.upsert(updated)
        self._store.audit(item.id, "enabled")
        return True

    def disable(self, credential_id: str) -> bool:
        """Mark a credential as disabled.  Returns True if found."""
        item = self._store.get_by_id(credential_id)
        if item is None:
            return False
        new_meta = dict(item.metadata)
        new_meta["enabled"] = False
        updated = VaultItem(
            id=item.id,
            kind=item.kind,
            label=item.label,
            provider=item.provider,
            encrypted_dek=item.encrypted_dek,
            encrypted_blob=item.encrypted_blob,
            metadata=new_meta,
            created_at=item.created_at,
            updated_at=datetime.now(timezone.utc),
            last_used_at=item.last_used_at,
            version=item.version + 1,
        )
        self._store.upsert(updated)
        self._store.audit(item.id, "disabled")
        return True

    def clone(
        self,
        credential_id: str,
        profile: str,
        label: str | None = None,
    ) -> str | None:
        """Clone a credential to a different profile.  Returns new ID, or None."""
        src_item = self._store.get_by_id(credential_id)
        if src_item is None:
            return None
        try:
            payload = self.get_bytes(src_item.label)
        except Exception:  # noqa: BLE001
            return None
        src_meta = dict(src_item.metadata)
        provider = src_item.provider or src_item.label.split("/")[0]
        new_meta = dict(src_meta)
        new_meta["profile_id"] = profile
        if label:
            new_meta["label"] = label
        new_label = _cred_label(provider, profile)
        return self.put(
            new_label, payload, kind=src_item.kind, provider=provider, metadata=new_meta
        )

    def list_profiles(self) -> list[str]:
        """Return all unique profile IDs across stored credentials."""
        items = self._store.list()
        profiles = {item.metadata.get("profile_id", "default") for item in items}
        profiles.add("default")
        return sorted(profiles)

    def get_active_profile(self) -> str:
        """Return the currently active profile (default: 'default')."""
        profile_file = self.vault_dir / "active_profile.txt"
        try:
            if profile_file.exists():
                return profile_file.read_text(encoding="utf-8").strip() or "default"
        except OSError:
            pass
        return "default"

    def set_active_profile(self, profile_id: str) -> None:
        """Persist the active profile selection."""
        profile_file = self.vault_dir / "active_profile.txt"
        try:
            self.vault_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_text(profile_file, profile_id or "default")
        except OSError:
            pass

    def get_audit_log(
        self,
        credential_id: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Return audit log entries, newest first.

        If *credential_id* is given, filters to that item only.
        """
        if credential_id:
            rows = self._store.get_audit(credential_id)
        else:
            try:
                with self._store._lock:  # shared connection — serialize like the store
                    conn = self._store._connect()
                    rows_raw = conn.execute(
                        "SELECT * FROM vault_audit ORDER BY ts DESC LIMIT ?", (limit,)
                    ).fetchall()
                rows = [dict(r) for r in rows_raw]
            except Exception:  # noqa: BLE001
                rows = []
        return rows[:limit]

    def list_creds(
        self,
        provider: str | None = None,
        profile_id: str | None = None,
        enabled_only: bool = False,
    ) -> list[CredentialInfo]:
        """List stored credentials as ``CredentialInfo`` objects.

        This is the compatibility listing method used by ``navig cred list``.
        """
        items = self.list(provider=provider, profile_id=profile_id)
        result = [_item_to_cred_info(i) for i in items]
        if enabled_only:
            result = [c for c in result if c.enabled]
        return result

    def test(self, credential_id: str) -> TestResult:
        """Validate a credential by its UUID against the provider's API."""
        cred = self.get_by_id(credential_id)
        if cred is None:
            return TestResult(success=False, message=f"Credential {credential_id} not found.")
        return self._run_validator(cred)

    def test_provider(
        self,
        provider: str,
        profile_id: str | None = None,
    ) -> TestResult:
        """Validate the credential for *provider* against the provider's API."""
        cred = self.get(provider, profile_id)
        if cred is None:
            return TestResult(
                success=False, message=f"No credential found for provider '{provider}'."
            )
        return self._run_validator(cred)

    def _run_validator(self, cred: Credential) -> TestResult:
        try:
            from . import validators as _validators_mod  # noqa: PLC0415

            validator = _validators_mod.get_validator(cred.provider)
            return validator.validate(cred)
        except Exception as exc:  # noqa: BLE001
            return TestResult(success=False, message=f"Validation error: {exc}")

    # ─────────────────────────────────────────────────────────────────────────
    # Legacy list() overload — preserves compatibility with call sites that
    # call vault.list(provider=..., profile_id=...) and iterate CredentialInfo.
    # ─────────────────────────────────────────────────────────────────────────

    # NOTE: list() already accepts profile_id kwarg and returns list[VaultItem].
    # Commands that need CredentialInfo (cred list / cred check-all) should call
    # list_creds() instead  — but we keep a dual-mode shim below so that old
    # code using `for c in vault.list(...): c.provider` still works because
    # VaultItem also has .provider.


# ── Backward-compat aliases ───────────────────────────────────────────────────


# ── Module-level singletons ───────────────────────────────────────────────────

_vault: Vault | None = None
# RLock, not Lock: _auto_migrate → migrate_from_legacy calls get_vault()
# re-entrantly on the constructing thread. With a plain Lock that self-
# deadlocks (proven by a faulthandler dump: core.py get_vault →
# _auto_migrate → migrate.py migrate_from_legacy → get_vault, blocked).
_vault_lock = threading.RLock()


def vault_exists(vault_dir_override: "Path | None" = None) -> bool:
    """Is there a vault on disk? A pure LOOK — it never creates one.

    :func:`get_vault` opens the store, and opening it CREATES the SQLite database (plus
    its -wal/-shm siblings) and the directory holding it. That is right for a caller
    about to store a secret and wrong for a caller merely ASKING whether a secret
    exists: read-only status probes (`navig init`'s summary, the provider source scan)
    were each leaving an empty encrypted store behind on machines that had never used
    the vault. Probe with this first.
    """
    from ._compat import vault_dir as _default_vault_dir  # noqa: PLC0415

    base = Path(vault_dir_override) if vault_dir_override is not None else _default_vault_dir()
    return (base / VaultStore.DB_FILE).is_file()


def get_vault(vault_dir: Path | None = None) -> Vault:
    """Return (or create) the global :class:`Vault` singleton.

    On first call, if a legacy database exists and has not yet been
    migrated, a silent migration is triggered automatically.

    Thread-safe: a parallel first use (e.g. several dispatch threads resolving
    credentials at once) must not construct two Vaults — each would carry its
    own sqlite connection and run the auto-migration concurrently.

    Parameters
    ----------
    vault_dir : Override the storage directory.  Uses ``vault_dir()`` from
                ``navig.platform.paths`` by default.
    """
    global _vault
    if _vault is None:
        with _vault_lock:
            if _vault is None:
                if vault_dir is None:
                    from ._compat import vault_dir as _vault_dir_fn  # noqa: PLC0415

                    vault_dir = _vault_dir_fn()
                # Assign BEFORE migrating (the original order): the migration
                # path re-enters get_vault() and must receive THIS instance —
                # assigning after would make the re-entrant call construct a
                # second Vault and re-run migration recursively.
                _vault = Vault(vault_dir)
                _auto_migrate(_vault)
    return _vault


def reveal_secret(vault: "Vault", label: str) -> str:
    """Fetch a vault secret as a plaintext ``str``, or ``""`` if missing/unavailable.

    :meth:`Vault.get_secret` returns a :class:`SecretStr`. Callers repeatedly wrote
    ``(vault.get_secret(label) or "").strip()`` — but ``.strip()`` on a ``SecretStr``
    raises ``AttributeError``, which their ``except`` clauses swallowed, so a present
    key was silently reported as absent (the whole reason vaulted search keys were
    never used). This unwraps via ``reveal()`` and never raises — a missing label or
    locked vault yields ``""``, letting callers keep their simple truthiness checks.
    """
    try:
        secret = vault.get_secret(label)
    except Exception:  # noqa: BLE001 — missing label / locked vault → treat as absent
        return ""
    if secret is None:
        return ""
    plain = secret.reveal() if hasattr(secret, "reveal") else str(secret)
    return plain.strip()


def _migrate_auth_profiles(vault: "Vault") -> None:
    """Migrate credentials from ``auth-profiles.json`` in *vault.vault_dir* into the vault.

    Called automatically when ``Vault(auto_migrate=True)`` is used.
    Renames the source file to ``auth-profiles.json.migrated`` on success.
    Never raises — migration failures are silently swallowed.
    """
    import json as _json  # noqa: PLC0415

    legacy = vault.vault_dir / "auth-profiles.json"
    if not legacy.exists():
        return
    try:
        data = _json.loads(legacy.read_text(encoding="utf-8"))
        for profile_id, cred in data.get("profiles", {}).items():
            provider = cred.get("provider", profile_id.split(":")[0])
            key = cred.get("key") or cred.get("api_key") or cred.get("token")
            if not key:
                continue
            path = _cred_label(provider, profile_id)
            if vault._store.get(path) is None:
                cred_data: dict = {"api_key": key}
                if cred.get("email"):
                    cred_data["email"] = cred["email"]
                vault.add(
                    provider=provider,
                    credential_type=cred.get("type", "api_key"),
                    data=cred_data,
                    profile_id=profile_id,
                    label="Migrated from auth-profiles.json",
                )
        legacy.rename(legacy.with_suffix(".json.migrated"))
    except Exception:  # noqa: BLE001
        pass  # Never crash on migration


def _maybe_upgrade_fingerprint(vault: Vault) -> None:
    """Silently re-key vault items when the machine fingerprint changed.

    This handles two upgrade scenarios transparently:

    1. **First run after the stable-fingerprint feature landed** (no
       ``.vault_fp`` file yet).  We probe whether the current master key can
       decrypt any stored item.  If it cannot, we try the legacy fingerprint
       (``CryptoEngine._legacy_fingerprint()``) and re-key if that works.

    2. **Subsequent runs where the fingerprint changed** (e.g. the operator
       switched from 32-bit to 64-bit Python on macOS or Linux).  The stored
       ``.vault_fp`` no longer matches; we re-key from the old fingerprint to
       the new one and update the file.

    All errors are silently swallowed — the vault does not raise on startup.
    """
    try:
        current_fp = CryptoEngine._machine_fingerprint()
        stored_fp = vault._read_stored_fingerprint()

        if vault.keyring.initialized():
            # Keyslot vault: items are sealed under the VMK, which a fingerprint
            # change does not touch — nothing to re-key. Only the fingerprint SLOT
            # goes stale; re-wrap it if another slot still opens the vault.
            if stored_fp != current_fp:
                if any(s.kind == FINGERPRINT for s in vault.keyring.slots()):
                    vmk = vault._master_key()
                    vault.keyring.add(vault.keyring.wrap(FINGERPRINT, vmk), vmk, replace_kind=True)
                vault._write_stored_fingerprint(current_fp)
            return

        if stored_fp is None:
            # First run: check whether current key actually works.
            items = vault._store.list()
            decryptable = 0
            for item in items[:3]:  # sample at most 3 items
                if not item.encrypted_dek:
                    continue
                try:
                    CryptoEngine.open(vault._master_key(), item.encrypted_dek)
                    decryptable += 1
                except CryptoError:
                    break
            if decryptable == 0 and any(i.encrypted_dek for i in items):
                # Current key cannot decrypt — attempt legacy fingerprint
                legacy_fp = CryptoEngine._legacy_fingerprint()
                if legacy_fp != current_fp:
                    n = vault._rekey_from_old_fingerprint(legacy_fp)
                    if n > 0:
                        vault._write_stored_fingerprint(current_fp)
                        return
            vault._write_stored_fingerprint(current_fp)

        elif stored_fp == current_fp:
            return  # fast path — fingerprint unchanged

        else:
            # Fingerprint changed between runs — re-key from stored → current.
            vault._rekey_from_old_fingerprint(stored_fp)
            vault._write_stored_fingerprint(current_fp)

    except Exception:  # noqa: BLE001
        pass  # never crash on startup due to fingerprint upgrade


def _auto_migrate(vault: Vault) -> None:
    """Silently migrate legacy credentials to the unified vault if not done yet."""
    _maybe_upgrade_fingerprint(vault)
    try:
        # Marker names + detection live in navig.vault.migrate (single source of
        # truth — the `navig doctor` Vault row reads the same helpers).
        from .migrate import (  # noqa: PLC0415
            check_legacy_exists,
            legacy_migration_done,
            migrate_from_legacy,
            migration_marker_path,
        )

        if legacy_migration_done(vault.vault_dir):
            return
        sentinel = migration_marker_path(vault.vault_dir)
        if not check_legacy_exists():
            sentinel.touch()
            return
        report = migrate_from_legacy()
        if report.ok() or report.migrated > 0:
            sentinel.touch()
    except Exception:  # noqa: BLE001
        pass  # Never crash on auto-migration; user can run navig vault migrate manually
