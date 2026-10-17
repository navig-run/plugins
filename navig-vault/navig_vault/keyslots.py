"""Keyslots — one Vault Master Key (VMK), wrapped once per way of unlocking it.

Before keyslots, the vault's master key WAS ``KDF(machine fingerprint)``: the
vault could not move to another machine, a hostname change re-keyed every item,
and there was no recovery path at all. Now every item DEK is sealed under a VMK,
and the VMK is stored only wrapped, in ``vault_keyslots``:

``dpapi``        Windows DPAPI, user scope — unlocks only as this Windows user.
``keyfile``      32 random bytes in an owner-only file (Linux/macOS device slot;
                 ``NAVIG_VAULT_DEVICE_KEY_FILE`` may point it at a systemd
                 credential). Same disk-theft exposure as ``fingerprint``, but it
                 survives a hostname change.
``fingerprint``  ``KDF(machine fingerprint, vault.salt)`` — the legacy key. Kept
                 so a migrated vault stays readable by older navig, and as a last
                 resort; ``navig vault status`` flags it as the weakest slot.
``passphrase``   ``KDF(passphrase, slot salt)`` with the KDF NAME recorded.
``recovery``     wrapped to an X25519 PUBLIC key. Its private half is printed once
                 and lives offline — it is never on this machine's disk, which is
                 what makes it a recovery path rather than another copy.

The first three unlock without a human ("auto" slots). A vault with no rows at
all is legacy and keeps working exactly as before until it is migrated.

Every change to slots happens in one SQLite transaction together with the
``vmk_check`` canary, so a crash never leaves a VMK that no slot can open.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .crypto import CryptoEngine, CryptoError

__all__ = [
    "AUTO_KINDS",
    "Keyring",
    "Keyslot",
    "VaultLocked",
    "format_recovery_key",
    "parse_recovery_key",
]

DPAPI, KEYFILE, FINGERPRINT, PASSPHRASE, RECOVERY = (
    "dpapi",
    "keyfile",
    "fingerprint",
    "passphrase",
    "recovery",
)
KINDS = (DPAPI, KEYFILE, FINGERPRINT, PASSPHRASE, RECOVERY)
#: Slots that open without a human, tried in this order (strongest first).
AUTO_KINDS = (DPAPI, KEYFILE, FINGERPRINT)

_VMK_CHECK = b"navig-vault-vmk/v1"
_VMK_CHECK_AAD = b"vmk-check"
_RECOVERY_INFO = b"navig-vault-recovery/v1"
_RK_PREFIX = "NAVIG-RK1-"

# VMKs already unwrapped in this process, keyed by (vault dir, vmk_check blob):
# the same trade the machine-key cache makes — every auto slot is re-openable by
# any code in the process, so re-unwrapping per read buys latency, not secrecy.
_VMK_CACHE: dict[tuple[str, bytes], bytes] = {}
_VMK_CACHE_LOCK = threading.Lock()


class VaultLocked(RuntimeError):
    """No keyslot this process can open without a passphrase or recovery key."""


@dataclass
class Keyslot:
    id: str
    kind: str
    wrapped: bytes
    params: dict = field(default_factory=dict)
    created_at: str = ""


def format_recovery_key(private_raw: bytes) -> str:
    """``NAVIG-RK1-XXXXX-XXXXX-…`` — base32, grouped so it can be copied by hand."""
    b32 = base64.b32encode(private_raw).decode("ascii").rstrip("=")
    return _RK_PREFIX + "-".join(b32[i : i + 5] for i in range(0, len(b32), 5))


def parse_recovery_key(text: str) -> bytes:
    """Inverse of :func:`format_recovery_key`; tolerant of spaces/case/dashes."""
    # Normalise FIRST (spaces, dashes, case) and only then drop the prefix, so a
    # key read back by hand as "navig rk1 aawbx …" parses like the printed one.
    raw = "".join(text.split()).upper().replace("-", "")
    prefix = _RK_PREFIX.replace("-", "")
    if raw.startswith(prefix):
        raw = raw[len(prefix) :]
    try:
        key = base64.b32decode(raw + "=" * (-len(raw) % 8))
    except ValueError as exc:  # binascii.Error is a ValueError
        raise ValueError("not a NAVIG recovery key") from exc
    if len(key) != 32:
        raise ValueError("not a NAVIG recovery key (wrong length)")
    return key


def _recipient_id(public_raw: bytes) -> str:
    return hashlib.sha256(public_raw).hexdigest()[:16]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def device_kind() -> str:
    """The device slot this platform uses: ``dpapi`` on Windows, else ``keyfile``."""
    return DPAPI if sys.platform == "win32" else KEYFILE


class Keyring:
    """Keyslot operations for one vault (its store, engine and directory)."""

    def __init__(self, store, engine: CryptoEngine, vault_dir: Path) -> None:
        self._store = store
        self._engine = engine
        self.vault_dir = Path(vault_dir)

    # ── Reading ──────────────────────────────────────────────────────────────

    def slots(self) -> list[Keyslot]:
        return [
            Keyslot(
                id=r["id"],
                kind=r["kind"],
                wrapped=bytes(r["wrapped"]),
                params=json.loads(r["params_json"] or "{}"),
                created_at=r["created_at"],
            )
            for r in self._store.keyslot_rows()
        ]

    def initialized(self) -> bool:
        return self._store.meta_get("vmk_check") is not None

    def _check(self, vmk: bytes) -> bool:
        blob = self._store.meta_get("vmk_check")
        if blob is None:
            return False
        try:
            return CryptoEngine.open(vmk, blob, aad=_VMK_CHECK_AAD) == _VMK_CHECK
        except CryptoError:
            return False

    # ── Protectors ───────────────────────────────────────────────────────────

    def keyfile_path(self) -> Path:
        override = os.environ.get("NAVIG_VAULT_DEVICE_KEY_FILE")
        return Path(override) if override else self.vault_dir / "device.key"

    def _keyfile_key(self, *, create: bool) -> bytes | None:
        path = self.keyfile_path()
        try:
            data = path.read_bytes()
            if len(data) == 32:
                return data
            return None
        except FileNotFoundError:
            if not create:
                return None
        from ._compat import set_owner_only_file_permissions  # noqa: PLC0415

        key = os.urandom(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        # tmp + replace: a torn key file would lock the keyfile slot for good.
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(key)
        set_owner_only_file_permissions(tmp)
        os.replace(tmp, path)
        return key

    def wrap(
        self,
        kind: str,
        vmk: bytes,
        *,
        passphrase: bytes | None = None,
        recipient_public: bytes | None = None,
    ) -> Keyslot:
        """A new (unsaved) slot of *kind* wrapping *vmk*."""
        params: dict = {}
        if kind == DPAPI:
            from . import dpapi  # noqa: PLC0415

            wrapped = dpapi.protect(vmk)
        elif kind == KEYFILE:
            key = self._keyfile_key(create=True)
            assert key is not None
            wrapped = CryptoEngine.seal(key, vmk, aad=b"keyslot:keyfile")
            params["path"] = str(self.keyfile_path())
        elif kind == FINGERPRINT:
            wrapped = CryptoEngine.seal(self._engine.derive_key(None), vmk, aad=b"keyslot:fp")
        elif kind == PASSPHRASE:
            if not passphrase:
                raise ValueError("a passphrase slot needs a passphrase")
            salt = os.urandom(16)
            kdf = CryptoEngine.current_kdf_name()
            kek = self._engine.kdf_named(kdf, passphrase, salt)
            wrapped = CryptoEngine.seal(kek, vmk, aad=b"keyslot:passphrase")
            params.update(kdf=kdf, salt=salt.hex())
        elif kind == RECOVERY:
            if not recipient_public or len(recipient_public) != 32:
                raise ValueError("a recovery slot needs a 32-byte X25519 public key")
            wrapped = CryptoEngine.seal_to(recipient_public, vmk, _RECOVERY_INFO)
            params.update(public=recipient_public.hex(), id=_recipient_id(recipient_public))
        else:
            raise ValueError(f"unknown keyslot kind {kind!r}")
        return Keyslot(id=str(uuid.uuid4()), kind=kind, wrapped=wrapped, params=params, created_at=_now())

    def _unwrap(self, slot: Keyslot, *, passphrase: bytes | None = None, recovery: bytes | None = None) -> bytes:
        if slot.kind == DPAPI:
            from . import dpapi  # noqa: PLC0415

            return dpapi.unprotect(slot.wrapped)
        if slot.kind == KEYFILE:
            key = self._keyfile_key(create=False)
            if key is None:
                raise CryptoError(f"device key file missing: {self.keyfile_path()}")
            return CryptoEngine.open(key, slot.wrapped, aad=b"keyslot:keyfile")
        if slot.kind == FINGERPRINT:
            return CryptoEngine.open(self._engine.derive_key(None), slot.wrapped, aad=b"keyslot:fp")
        if slot.kind == PASSPHRASE:
            if passphrase is None:
                raise CryptoError("passphrase required")
            kek = self._engine.kdf_named(
                slot.params.get("kdf", "argon2id"), passphrase, bytes.fromhex(slot.params["salt"])
            )
            return CryptoEngine.open(kek, slot.wrapped, aad=b"keyslot:passphrase")
        if slot.kind == RECOVERY:
            if recovery is None:
                raise CryptoError("recovery key required")
            return CryptoEngine.open_from(recovery, slot.wrapped, _RECOVERY_INFO)
        raise CryptoError(f"unknown keyslot kind {slot.kind!r}")

    # ── Unlocking ────────────────────────────────────────────────────────────

    def unlock_auto(self) -> bytes:
        """The VMK through the first auto slot that opens. Raises :class:`VaultLocked`."""
        check = self._store.meta_get("vmk_check")
        cache_key = (str(self.vault_dir.resolve()), check or b"")
        with _VMK_CACHE_LOCK:
            cached = _VMK_CACHE.get(cache_key)
        if cached is not None:
            return cached
        failures: list[str] = []
        slots = self.slots()
        for kind in AUTO_KINDS:
            for slot in (s for s in slots if s.kind == kind):
                try:
                    vmk = self._unwrap(slot)
                except Exception as exc:  # noqa: BLE001 — try the next slot, report all
                    failures.append(f"{kind}: {type(exc).__name__}")
                    continue
                if self._check(vmk):
                    with _VMK_CACHE_LOCK:
                        _VMK_CACHE[cache_key] = vmk
                    return vmk
                failures.append(f"{kind}: wrong key")
        have = sorted({s.kind for s in slots})
        raise VaultLocked(
            "the vault is locked — no automatic keyslot opens on this machine "
            f"(slots: {', '.join(have) or 'none'}; tried: {'; '.join(failures) or 'none'}). "
            "Unlock with a passphrase or restore with the recovery key."
        )

    def unlock_with(self, kind: str, secret: bytes) -> bytes:
        """The VMK through a ``passphrase`` or ``recovery`` slot."""
        for slot in (s for s in self.slots() if s.kind == kind):
            try:
                vmk = self._unwrap(
                    slot,
                    passphrase=secret if kind == PASSPHRASE else None,
                    recovery=secret if kind == RECOVERY else None,
                )
            except CryptoError:
                continue
            if self._check(vmk):
                return vmk
        if not any(s.kind == kind for s in self.slots()):
            raise VaultLocked(f"this vault has no {kind} keyslot")
        raise VaultLocked(f"that {kind} does not open this vault")

    # ── Writing (each one transaction) ───────────────────────────────────────

    def _insert(self, conn, slot: Keyslot) -> None:
        conn.execute(
            "INSERT INTO vault_keyslots (id, kind, wrapped, params_json, created_at) VALUES (?,?,?,?,?)",
            (slot.id, slot.kind, slot.wrapped, json.dumps(slot.params), slot.created_at),
        )

    def initialize(self, vmk: bytes, *, kinds: tuple[str, ...] | None = None) -> list[Keyslot]:
        """Write the canary and the default slots for *vmk*. Refuses if already initialized.

        Default slots: this platform's device slot plus ``fingerprint``. A device
        slot that cannot be created here (no DPAPI) is skipped, never fatal — the
        fingerprint slot always works.
        """
        if self.initialized():
            raise RuntimeError("vault keyslots are already initialized")
        made: list[Keyslot] = []
        for kind in kinds or (device_kind(), FINGERPRINT):
            try:
                made.append(self.wrap(kind, vmk))
            except Exception:  # noqa: BLE001 — a missing protector is not fatal
                if kind == FINGERPRINT:
                    raise
        with self._store.transaction() as conn:
            conn.execute(
                "INSERT INTO vault_meta (key, value) VALUES ('vmk_check', ?)",
                (CryptoEngine.seal(vmk, _VMK_CHECK, aad=_VMK_CHECK_AAD),),
            )
            for slot in made:
                self._insert(conn, slot)
        return made

    def add(self, slot: Keyslot, vmk: bytes, *, replace_kind: bool = False) -> None:
        """Save *slot* (it must wrap *vmk*); ``replace_kind`` drops older slots of its kind."""
        if not self._check(vmk):
            raise CryptoError("refusing to add a keyslot for a key that is not this vault's VMK")
        with self._store.transaction() as conn:
            if replace_kind:
                conn.execute("DELETE FROM vault_keyslots WHERE kind = ?", (slot.kind,))
            self._insert(conn, slot)

    def remove(self, kind: str) -> int:
        """Delete every slot of *kind*. Refuses to remove the last way in.

        After removal at least one auto slot must remain, OR a recovery slot plus
        a passphrase slot — otherwise the vault could become unopenable.
        """
        remaining = [s for s in self.slots() if s.kind != kind]
        if not any(s.kind in AUTO_KINDS for s in remaining) and not any(
            s.kind == PASSPHRASE for s in remaining
        ):
            raise RuntimeError(
                f"refusing to remove the {kind} slot: nothing else would unlock this vault"
            )
        with self._store.transaction() as conn:
            cur = conn.execute("DELETE FROM vault_keyslots WHERE kind = ?", (kind,))
            n = cur.rowcount
        with _VMK_CACHE_LOCK:
            _VMK_CACHE.clear()
        return n

    def rewrap_all(self, conn, new_vmk: bytes, *, passphrase: bytes | None = None) -> list[str]:
        """Replace every slot + the canary for *new_vmk*, inside the caller's transaction.

        Returns the kinds that could NOT be re-wrapped (a passphrase slot without
        the passphrase) — those slots are DROPPED, never left wrapping a dead key.
        """
        dropped: list[str] = []
        new_slots: list[Keyslot] = []
        for slot in self.slots():
            if slot.kind == PASSPHRASE and passphrase is None:
                dropped.append(PASSPHRASE)
                continue
            if slot.kind == RECOVERY:
                new_slots.append(
                    self.wrap(RECOVERY, new_vmk, recipient_public=bytes.fromhex(slot.params["public"]))
                )
            else:
                new_slots.append(self.wrap(slot.kind, new_vmk, passphrase=passphrase))
        conn.execute("DELETE FROM vault_keyslots")
        conn.execute(
            "INSERT OR REPLACE INTO vault_meta (key, value) VALUES ('vmk_check', ?)",
            (CryptoEngine.seal(new_vmk, _VMK_CHECK, aad=_VMK_CHECK_AAD),),
        )
        for slot in new_slots:
            self._insert(conn, slot)
        return dropped


def forget_cached_vmks() -> None:
    """Drop every VMK this process unwrapped (lock, rotation, tests)."""
    with _VMK_CACHE_LOCK:
        _VMK_CACHE.clear()
