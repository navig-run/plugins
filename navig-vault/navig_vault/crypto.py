"""NAVIG Vault Cryptographic Engine — AES-256-GCM + Argon2id.

Encryption : AES-256-GCM (authenticated, 128-bit tag, 12-byte nonce)
KDF        : Argon2id — RFC 9106 interactive profile (m=65536, t=3, p=4)
Fallback   : PBKDF2HMAC-SHA256 @ 600 000 iter when argon2-cffi not installed

Key hierarchy
-------------
Keyslots  (dpapi · keyfile · fingerprint · passphrase · recovery — see keyslots.py)
  └─ Vault Master Key (VMK)  — 32 random bytes; legacy vaults: the fingerprint key
       └─ Per-item DEK  (32-byte random, encrypted with the VMK via AES-GCM)
            └─ Item payload  (encrypted with DEK via AES-GCM)

A vault with no keyslot rows is LEGACY: its master key is derived directly from
the passphrase or the machine fingerprint, as before keyslots existed.

Compromising one DEK exposes only that item.
"""

from __future__ import annotations

import os
import platform
import socket
import threading
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from ._compat import set_owner_only_file_permissions

# argon2-cffi is probed lazily on the first KDF call rather than at module
# import time.  On some platforms (Windows / Python 3.14) the argon2-cffi C
# extension blocks indefinitely during DLL loading, which hangs the entire
# navig.vault import chain and causes pytest collection to stall.
_argon2_probed: bool = False
_argon2_funcs: tuple | None = None  # (Type, hash_secret_raw) when argon2 is available

__all__ = ["CryptoEngine", "CryptoError"]

# ── Constants ────────────────────────────────────────────────────────────────
_NONCE_LEN = 12  # AES-GCM recommended nonce size
_KEY_LEN = 32  # AES-256
_SALT_LEN = 32  # KDF salt
_A2_TIME_COST = 3
_A2_MEMORY_COST = 65_536  # 64 MiB — RFC 9106 interactive
_A2_PARALLELISM = 4
_A2_SALT_PAD = 16  # argon2 requires salt ≥ 8; use 16 for alignment
_PBKDF2_ITERS = 600_000  # OWASP 2023 minimum for PBKDF2-HMAC-SHA256

# Machine-fingerprint master keys, keyed by (salt, fingerprint material).
#
# In machine-fingerprint mode (no passphrase) the master key is a deterministic
# function of the vault salt and this machine's identity — any code in the
# process can re-derive it at will, so re-running Argon2id on every secret read
# buys no secrecy, only latency: ~350 ms per read, and listing AI connections
# reads one secret per configured provider, so every agent turn paid 1-2 s here.
# Keying on the salt AND the material means a rotated/new salt or a changed
# machine identity derives afresh instead of reusing a stale key. Passphrase
# keys are deliberately NOT cached here: their lifetime is the explicit,
# TTL-bound unlock session (SessionStore), which this must not extend.
_MACHINE_KEY_CACHE: dict[tuple[bytes, bytes], bytes] = {}
_MACHINE_KEY_LOCK = threading.Lock()


class CryptoError(Exception):
    """Raised when any crypto operation fails (decryption, auth, bad data)."""


class CryptoEngine:
    """Low-level cryptographic primitives for the vault.

    One instance per vault directory.  Manages the per-vault salt file and
    provides all encrypt/decrypt primitives.

    Usage
    -----
    engine = CryptoEngine(vault_dir)
    master = engine.derive_key()          # machine-fingerprint mode
    master = engine.derive_key(b"pass")   # passphrase mode

    dek = CryptoEngine.generate_dek()
    blob = CryptoEngine.seal(dek, b"secret payload")
    payload = CryptoEngine.open(dek, blob)
    """

    SALT_FILE = "vault.salt"

    def __init__(self, vault_dir: Path) -> None:
        self.vault_dir = vault_dir
        self._salt: bytes | None = None

    # ── Salt management ──────────────────────────────────────────────────────

    def _get_or_create_salt(self) -> bytes:
        if self._salt is not None:
            return self._salt
        salt_path = self.vault_dir / self.SALT_FILE
        if salt_path.exists():
            data = salt_path.read_bytes()
            if len(data) < 16:
                raise CryptoError(f"Salt file corrupt (only {len(data)} bytes): {salt_path}")
            self._salt = data
        else:
            self._salt = os.urandom(_SALT_LEN)
            self.vault_dir.mkdir(parents=True, exist_ok=True)
            salt_path.write_bytes(self._salt)
            set_owner_only_file_permissions(salt_path)
        return self._salt

    # ── Key derivation ────────────────────────────────────────────────────────

    def derive_key(self, passphrase: bytes | None = None) -> bytes:
        """Derive a 256-bit master key.

        Parameters
        ----------
        passphrase:
            Raw bytes of user passphrase.  If ``None``, derives from the
            machine fingerprint (non-interactive / daemon mode).

        Returns
        -------
        bytes
            32-byte master key suitable for AES-256-GCM.
        """
        salt = self._get_or_create_salt()
        if passphrase is not None:
            return self._kdf(passphrase, salt)
        material = self._machine_fingerprint()
        cache_key = (salt, material)
        # Held across the derivation so concurrent first reads (a parallel
        # agent fan-out) run Argon2 — 64 MiB each — once, not N times.
        with _MACHINE_KEY_LOCK:
            key = _MACHINE_KEY_CACHE.get(cache_key)
            if key is None:
                key = self._kdf(material, salt)
                _MACHINE_KEY_CACHE[cache_key] = key
            return key

    @staticmethod
    def _probe_argon2() -> None:
        global _argon2_probed, _argon2_funcs
        if not _argon2_probed:
            _argon2_probed = True
            try:
                from argon2.low_level import Type as _A2T  # noqa: PLC0415
                from argon2.low_level import hash_secret_raw as _hsr
                _argon2_funcs = (_A2T, _hsr)
            except Exception:  # noqa: BLE001  — catches ImportError and DLL load failures
                _argon2_funcs = None

    @classmethod
    def current_kdf_name(cls) -> str:
        """``"argon2id"`` when argon2-cffi loads, else ``"pbkdf2"`` — what :meth:`_kdf` uses."""
        cls._probe_argon2()
        return "argon2id" if _argon2_funcs is not None else "pbkdf2"

    def kdf_named(self, name: str, material: bytes, salt: bytes) -> bytes:
        """Derive with an EXPLICIT KDF, so a keyslot sealed with one can be opened later.

        :meth:`_kdf` silently picks whichever KDF is importable, which means a vault
        sealed under Argon2id becomes unopenable the day ``argon2-cffi`` fails to load.
        Keyslots record the name and call this instead; a missing KDF is an error that
        says so, not a wrong key.
        """
        if name == "pbkdf2":
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(), length=_KEY_LEN, salt=salt, iterations=_PBKDF2_ITERS
            )
            return kdf.derive(material)
        if name == "argon2id":
            self._probe_argon2()
            if _argon2_funcs is None:
                raise CryptoError(
                    "this keyslot was sealed with Argon2id, but argon2-cffi is not "
                    "installed here (pip install argon2-cffi)"
                )
            return self._kdf(material, salt)
        raise CryptoError(f"unknown KDF {name!r}")

    def _kdf(self, material: bytes, salt: bytes) -> bytes:
        self._probe_argon2()
        if _argon2_funcs is not None:
            _A2Type, hash_secret_raw = _argon2_funcs
            # argon2 requires salt len ≥ 8 bytes; we pad to 16
            padded_salt = (salt + b"\x00" * _A2_SALT_PAD)[:_A2_SALT_PAD]
            return hash_secret_raw(
                secret=material,
                salt=padded_salt,
                time_cost=_A2_TIME_COST,
                memory_cost=_A2_MEMORY_COST,
                parallelism=_A2_PARALLELISM,
                hash_len=_KEY_LEN,
                type=_A2Type.ID,
            )
        # Fallback: PBKDF2-HMAC-SHA256
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=_KEY_LEN,
            salt=salt,
            iterations=_PBKDF2_ITERS,
        )
        return kdf.derive(material)

    @staticmethod
    def _os_arch() -> str:
        """Return the hardware architecture, stable across 32-bit and 64-bit Python.

        On POSIX, ``platform.machine()`` reflects the *Python binary's* bitness,
        not the kernel's.  A 32-bit Python on a 64-bit Linux kernel reports
        ``i686`` while a 64-bit Python reports ``x86_64``.  On Apple Silicon,
        Rosetta-translated x86_64 Python reports ``x86_64`` while native ARM
        Python reports ``arm64``.  In both cases ``os.uname().machine`` returns
        the kernel's view (``x86_64`` / ``arm64``) regardless of Python bitness.

        On Windows, ``platform.machine()`` already returns the CPU architecture
        (``AMD64``) for both 32-bit and 64-bit Python, so no correction is needed.
        """
        if platform.system() != "Windows":
            try:
                return os.uname().machine  # pylint: disable=no-member
            except AttributeError:
                pass  # should not happen on any POSIX platform; fall through
        return platform.machine()

    @staticmethod
    def _stable_machine_uuid() -> str | None:
        """Return a stable OS-level machine UUID, or ``None`` if unavailable.

        Sources per platform:

        - **Linux**   : ``/etc/machine-id`` (systemd) or
          ``/var/lib/dbus/machine-id`` — a file read, no subprocess needed.
        - **macOS**   : ``IOPlatformUUID`` from ``ioreg -rd1 -c
          IOPlatformExpertDevice`` — survives OS upgrades, unique per logic-board.
        - **Windows** : ``MachineGuid`` from
          ``HKLM\\SOFTWARE\\Microsoft\\Cryptography`` opened with
          ``KEY_WOW64_64KEY`` so that 32-bit Python on a 64-bit OS bypasses
          WOW64 registry redirection and reads the same value as 64-bit Python.

        All errors are silently swallowed — the UUID is *extra* fingerprint
        entropy and the vault remains functional without it.
        """
        system = platform.system()
        try:
            if system == "Linux":
                for _mid in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
                    _p = Path(_mid)
                    if _p.exists():
                        uid = _p.read_text(encoding="utf-8").strip()
                        if uid:
                            return uid
            elif system == "Darwin":
                import subprocess  # noqa: PLC0415

                result = subprocess.run(  # noqa: S603,S607
                    ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                for line in result.stdout.splitlines():
                    if "IOPlatformUUID" in line:
                        _parts = line.split("=", 1)
                        if len(_parts) == 2:
                            uid = _parts[1].strip().strip('"')
                            if uid:
                                return uid
            elif system == "Windows":
                import winreg  # noqa: PLC0415

                # KEY_WOW64_64KEY forces 32-bit Python to bypass WOW64
                # redirection so it reads the same path as 64-bit Python.
                key = winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"SOFTWARE\Microsoft\Cryptography",
                    0,
                    winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
                )
                guid, _ = winreg.QueryValueEx(key, "MachineGuid")
                if guid:
                    return str(guid)
        except Exception:  # noqa: BLE001
            pass  # best-effort; vault works without a UUID
        return None

    @staticmethod
    def _machine_fingerprint() -> bytes:
        """Derive stable per-machine key material (no passphrase required).

        Robust across 32-bit / 64-bit Python on the same OS and across
        Rosetta-translated vs. native Python on Apple Silicon:

        - Uses ``_os_arch()`` (kernel arch) instead of ``platform.machine()``
          (Python bitness) so 32-bit and 64-bit Python produce the same result.
        - Appends a stable OS-level UUID (``_stable_machine_uuid()``) when
          available, providing stronger binding to the physical machine.
        """
        parts = [
            platform.node(),
            socket.gethostname(),
            platform.system(),
            CryptoEngine._os_arch(),
        ]
        uid = CryptoEngine._stable_machine_uuid()
        if uid:
            parts.append(uid)
        return "-".join(p for p in parts if p).encode()

    @staticmethod
    def _legacy_fingerprint() -> bytes:
        """Reconstruct the fingerprint as produced by the pre-cross-platform code.

        Used by the auto-migration path in ``navig.vault.core`` to re-key
        vaults that were created before the stable UUID / arch fix.

        The old code used ``platform.machine()`` (Python bitness) and added
        a Windows ``MachineGuid`` only when the old registry call succeeded
        (no ``KEY_WOW64_64KEY`` — so 32-bit Python on 64-bit Windows got no
        GUID).  We cannot know which variant was in effect at write time, so
        the migration logic probes both variants; this method returns the
        no-UUID base, and the migration tries appending it separately.
        """
        parts = [
            platform.node(),
            socket.gethostname(),
            platform.system(),
            platform.machine(),  # intentional: reproduce old behaviour
        ]
        return "-".join(p for p in parts if p).encode()

    @staticmethod
    def kdf_info() -> str:
        """Human-readable description of the active KDF (for navig vault doctor)."""
        if _argon2_funcs is not None:
            return (
                f"Argon2id  m={_A2_MEMORY_COST // 1024}MiB  t={_A2_TIME_COST}  p={_A2_PARALLELISM}"
            )
        return f"PBKDF2-HMAC-SHA256  iter={_PBKDF2_ITERS:,}  (install argon2-cffi for Argon2id)"

    # ── AES-256-GCM primitives ────────────────────────────────────────────────

    @staticmethod
    def generate_dek() -> bytes:
        """Generate a cryptographically random 256-bit Data Encryption Key."""
        return os.urandom(_KEY_LEN)

    @staticmethod
    def encrypt(key: bytes, plaintext: bytes, aad: bytes = b"") -> tuple[bytes, bytes]:
        """Encrypt *plaintext* with AES-256-GCM.

        Parameters
        ----------
        key       : 32-byte encryption key
        plaintext : data to encrypt
        aad       : additional authenticated data (not encrypted, but verified)

        Returns
        -------
        (nonce, ciphertext_with_tag)
            12-byte nonce and ciphertext that includes the 16-byte auth tag.
        """
        nonce = os.urandom(_NONCE_LEN)
        ct = AESGCM(key).encrypt(nonce, plaintext, aad if aad else None)
        return nonce, ct

    @staticmethod
    def decrypt(key: bytes, nonce: bytes, ciphertext: bytes, aad: bytes = b"") -> bytes:
        """Decrypt AES-256-GCM ciphertext.

        Raises :class:`CryptoError` on authentication failure or wrong key.
        """
        try:
            return AESGCM(key).decrypt(nonce, ciphertext, aad if aad else None)
        except Exception as exc:
            raise CryptoError("Decryption failed — wrong key or tampered data") from exc

    @classmethod
    def seal(cls, key: bytes, plaintext: bytes, aad: bytes = b"") -> bytes:
        """Encrypt and return a self-contained blob: ``nonce ‖ ciphertext``.

        Use :meth:`open` to reverse.
        """
        nonce, ct = cls.encrypt(key, plaintext, aad)
        return nonce + ct

    @classmethod
    def open(cls, key: bytes, blob: bytes, aad: bytes = b"") -> bytes:
        """Decrypt a blob produced by :meth:`seal`.

        Raises :class:`CryptoError` if the blob is too short or auth fails.
        """
        min_len = _NONCE_LEN + 16  # nonce + tag minimum
        if len(blob) < min_len:
            raise CryptoError(f"Blob too short ({len(blob)} bytes) — minimum is {min_len}")
        return cls.decrypt(key, blob[:_NONCE_LEN], blob[_NONCE_LEN:], aad)

    # ── Public-key wrapping (recovery keyslot, backups) ───────────────────────
    #
    # X25519 + HKDF-SHA256 + AES-256-GCM: anyone holding the PUBLIC key can wrap,
    # only the holder of the private key can unwrap. That is what lets a backup be
    # written unattended by a daemon that never sees the recovery key.

    @staticmethod
    def x25519_generate() -> tuple[bytes, bytes]:
        """A fresh ``(private_raw, public_raw)`` X25519 pair, 32 bytes each."""
        from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
        from cryptography.hazmat.primitives.asymmetric.x25519 import (  # noqa: PLC0415
            X25519PrivateKey,
        )

        priv = X25519PrivateKey.generate()
        raw = serialization.Encoding.Raw
        return (
            priv.private_bytes(raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()),
            priv.public_key().public_bytes(raw, serialization.PublicFormat.Raw),
        )

    @staticmethod
    def x25519_public(private_raw: bytes) -> bytes:
        from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
        from cryptography.hazmat.primitives.asymmetric.x25519 import (  # noqa: PLC0415
            X25519PrivateKey,
        )

        return (
            X25519PrivateKey.from_private_bytes(private_raw)
            .public_key()
            .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        )

    @staticmethod
    def _recipient_kek(shared: bytes, eph_pub: bytes, recipient_pub: bytes, info: bytes) -> bytes:
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF  # noqa: PLC0415

        return HKDF(
            algorithm=hashes.SHA256(), length=_KEY_LEN, salt=None, info=info + eph_pub + recipient_pub
        ).derive(shared)

    @classmethod
    def seal_to(cls, recipient_pub: bytes, plaintext: bytes, info: bytes) -> bytes:
        """Encrypt to an X25519 public key: ``eph_pub(32) ‖ seal(kek, plaintext)``."""
        from cryptography.hazmat.primitives.asymmetric.x25519 import (  # noqa: PLC0415
            X25519PrivateKey,
            X25519PublicKey,
        )

        from cryptography.hazmat.primitives import serialization  # noqa: PLC0415

        eph = X25519PrivateKey.generate()
        eph_pub = eph.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        shared = eph.exchange(X25519PublicKey.from_public_bytes(recipient_pub))
        kek = cls._recipient_kek(shared, eph_pub, recipient_pub, info)
        return eph_pub + cls.seal(kek, plaintext, aad=info)

    @classmethod
    def open_from(cls, private_raw: bytes, blob: bytes, info: bytes) -> bytes:
        """Reverse :meth:`seal_to` with the recipient's private key."""
        from cryptography.hazmat.primitives.asymmetric.x25519 import (  # noqa: PLC0415
            X25519PrivateKey,
            X25519PublicKey,
        )

        if len(blob) < 32 + _NONCE_LEN + 16:
            raise CryptoError("recipient blob too short")
        eph_pub, sealed = blob[:32], blob[32:]
        priv = X25519PrivateKey.from_private_bytes(private_raw)
        recipient_pub = cls.x25519_public(private_raw)
        shared = priv.exchange(X25519PublicKey.from_public_bytes(eph_pub))
        kek = cls._recipient_kek(shared, eph_pub, recipient_pub, info)
        return cls.open(kek, sealed, aad=info)
