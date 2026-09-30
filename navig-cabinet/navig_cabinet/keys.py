"""The cabinet's key file: one random master key, wrapped by a key-encryption key.

    master key   32 random bytes, generated once, never changes
      └─ wrapped by the KEK   (AES-256-GCM)
            KEK = scrypt(material, salt)
              material = this machine's identity   — mode "machine" (default)
                       | a passphrase you choose     — mode "passphrase"

Switching between the two modes re-wraps only the master key. Every item's data key
is wrapped by the master key, so no stored file is re-encrypted — switching is instant
even with gigabytes in the cabinet.

Two properties are deliberate, and both differ from navig-vault:

* **A wrong passphrase fails.** It never falls back to the machine key. A lock that
  quietly opens with a different key is not a lock.
* **The KDF is recorded, not inferred.** scrypt ships inside ``cryptography`` (no
  optional C extension that may or may not be importable today), and its parameters
  are written into ``key.json`` — so installing a package later cannot change which
  key a passphrase derives to.

Machine mode binds to the OS machine id (``MachineGuid`` / ``/etc/machine-id`` /
``IOPlatformUUID``) when there is one, not the hostname: renaming the computer must
not lock you out of your passport scan. It still does not survive a reinstall — which
is what ``navig cabinet backup`` is for.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

KEY_FILE = "key.json"
FORMAT = 1
_WRAP_AAD = b"navig-cabinet/master-key/v1"

# scrypt cost. 2**17 * 8 * 128 B = 128 MiB and ~0.3-0.6 s per derivation: a guessing
# attack against a stolen key.json pays that per guess. Machine mode's material is not
# guessable by a person, so it uses a cheaper setting to keep every command snappy.
# Tests lower both through these module attributes; the value used is always recorded.
PASSPHRASE_N = 2**17
MACHINE_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1


class KeyError_(Exception):
    """Base class for key problems — named with a trailing underscore to not shadow ``KeyError``."""


class PassphraseRequired(KeyError_):
    """The cabinet is passphrase-protected and no passphrase was given."""


class WrongKey(KeyError_):
    """The passphrase (or this machine) does not open the cabinet."""


class NotInitialised(KeyError_):
    """There is no cabinet here yet."""


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _unb64(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def derive(material: bytes, salt: bytes, n: int, r: int = SCRYPT_R, p: int = SCRYPT_P) -> bytes:
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(material)


def machine_material() -> tuple[bytes, str]:
    """This machine's identity as key material, and which source it came from."""
    from navig_vault.crypto import CryptoEngine

    uid = CryptoEngine._stable_machine_uuid()
    if uid:
        return b"navig-cabinet/machine-id/" + uid.encode("utf-8"), "machine-id"
    return b"navig-cabinet/fingerprint/" + CryptoEngine._machine_fingerprint(), "fingerprint"


@dataclass
class KeyFile:
    mode: str                  # "machine" | "passphrase"
    salt: bytes
    n: int
    r: int
    p: int
    wrapped: bytes             # nonce ‖ AES-GCM(master)
    machine_source: str | None = None

    # ── persistence ────────────────────────────────────────────────────────
    @classmethod
    def path(cls, root: Path) -> Path:
        return root / KEY_FILE

    @classmethod
    def exists(cls, root: Path) -> bool:
        return cls.path(root).is_file()

    @classmethod
    def load(cls, root: Path) -> KeyFile:
        p = cls.path(root)
        if not p.is_file():
            raise NotInitialised(f"no cabinet at {root}")
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            kdf = d["kdf"]
            if d.get("format") != FORMAT or kdf.get("name") != "scrypt":
                raise KeyError_(f"unsupported key file format in {p}")
            return cls(
                mode=d["mode"], salt=_unb64(kdf["salt"]), n=int(kdf["n"]), r=int(kdf["r"]),
                p=int(kdf["p"]), wrapped=_unb64(d["wrapped_key"]),
                machine_source=d.get("machine_source"),
            )
        except (ValueError, KeyError, TypeError) as exc:
            # The key file is the one file whose loss loses everything; say so plainly
            # rather than letting a KeyError escape as if it were a missing dict entry.
            raise KeyError_(f"key file {p} is unreadable: {exc}") from exc

    def save(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        body = {
            "format": FORMAT,
            "mode": self.mode,
            "kdf": {"name": "scrypt", "n": self.n, "r": self.r, "p": self.p, "salt": _b64(self.salt)},
            "wrapped_key": _b64(self.wrapped),
        }
        if self.machine_source:
            body["machine_source"] = self.machine_source
        p = self.path(root)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
        _owner_only(tmp)
        os.replace(tmp, p)

    # ── wrapping ───────────────────────────────────────────────────────────
    def _kek(self, passphrase: str | None) -> bytes:
        if self.mode == "passphrase":
            if passphrase is None:
                raise PassphraseRequired("this cabinet is locked with a passphrase")
            return derive(passphrase.encode("utf-8"), self.salt, self.n, self.r, self.p)
        material, source = machine_material()
        if self.machine_source and source != self.machine_source:
            raise WrongKey(
                f"this cabinet was bound to the machine's {self.machine_source}, which is not "
                "available now — restore from a backup"
            )
        return derive(material, self.salt, self.n, self.r, self.p)

    def unwrap(self, passphrase: str | None = None) -> bytes:
        kek = self._kek(passphrase)
        try:
            return AESGCM(kek).decrypt(self.wrapped[:12], self.wrapped[12:], _WRAP_AAD)
        except Exception as exc:  # noqa: BLE001 — InvalidTag
            if self.mode == "passphrase":
                raise WrongKey("wrong passphrase") from exc
            raise WrongKey(
                "this machine's key does not open the cabinet (was it copied from another "
                "computer, or was Windows reinstalled?) — restore from a backup"
            ) from exc


def _wrap(kek: bytes, master: bytes) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(kek).encrypt(nonce, master, _WRAP_AAD)


def _owner_only(path: Path) -> None:
    try:
        from navig_vault._compat import set_owner_only_file_permissions

        set_owner_only_file_permissions(path)
    except Exception:  # noqa: BLE001 — best effort, same as the vault
        pass


def create(root: Path) -> bytes:
    """Initialise a machine-mode cabinet. Returns the new master key."""
    if KeyFile.exists(root):
        raise KeyError_(f"a cabinet already exists at {root}")
    master = os.urandom(32)
    _rewrap(root, master, passphrase=None)
    return master


def _rewrap(root: Path, master: bytes, passphrase: str | None) -> KeyFile:
    salt = os.urandom(16)
    if passphrase is None:
        material, source = machine_material()
        kf = KeyFile(mode="machine", salt=salt, n=MACHINE_N, r=SCRYPT_R, p=SCRYPT_P,
                     wrapped=b"", machine_source=source)
    else:
        material, source = passphrase.encode("utf-8"), None
        kf = KeyFile(mode="passphrase", salt=salt, n=PASSPHRASE_N, r=SCRYPT_R, p=SCRYPT_P,
                     wrapped=b"")
    kf.wrapped = _wrap(derive(material, salt, kf.n, kf.r, kf.p), master)
    kf.save(root)
    # Prove the new file opens before anyone relies on it.
    if KeyFile.load(root).unwrap(passphrase) != master:  # pragma: no cover — defensive
        raise KeyError_("key file failed its own read-back")
    return kf


def set_passphrase(root: Path, master: bytes, passphrase: str) -> None:
    if not passphrase:
        raise ValueError("passphrase must not be empty")
    _rewrap(root, master, passphrase)


def clear_passphrase(root: Path, master: bytes) -> None:
    _rewrap(root, master, None)
