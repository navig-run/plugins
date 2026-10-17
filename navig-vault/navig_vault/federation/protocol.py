"""Vault federation wire protocol — signing is stdlib-only, so NAVIG Mini can speak it.

Every device request carries four headers and NO bearer token:

    X-Navig-Device  <device id>
    X-Navig-Ts      <unix seconds>
    X-Navig-Nonce   <32 hex chars, single use>
    X-Navig-Sig     hex HMAC-SHA256(device key, canonical request)

    canonical = METHOD \n PATH \n TS \n NONCE \n hex sha256(body)

A signature proves possession of the device key without ever sending it, binds
the body, and — with the authority's ±``SKEW_S`` window plus a nonce cache —
cannot be replayed. Nothing in a log line or a proxy hop is a credential.

Secret values in responses are SEALED to the device: AES-256-GCM under
``HKDF(device key, info = SEAL_INFO ‖ request nonce)`` with the label and
version as associated data. A relay that carries the response sees ciphertext
it cannot open and cannot move to another label. Sealing needs the
``cryptography`` package; a client without it may ask for ``none`` and the
authority honours that ONLY for a request that did not arrive through a relay.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

__all__ = [
    "H_DEVICE",
    "H_NONCE",
    "H_SEAL",
    "H_SIG",
    "H_TS",
    "SKEW_S",
    "canonical",
    "open_value",
    "seal_value",
    "sign",
    "verify_signature",
]

H_DEVICE = "X-Navig-Device"
H_TS = "X-Navig-Ts"
H_NONCE = "X-Navig-Nonce"
H_SIG = "X-Navig-Sig"
H_SEAL = "X-Navig-Seal"  # "aes-gcm" (default) | "none"

SKEW_S = 60
SEAL_INFO = b"navig-vault-fed-seal/v1"


def canonical(method: str, path: str, ts: str, nonce: str, body: bytes) -> bytes:
    return "\n".join(
        (method.upper(), path, ts, nonce, hashlib.sha256(body or b"").hexdigest())
    ).encode("utf-8")


def sign(
    device_id: str,
    device_key: bytes,
    method: str,
    path: str,
    body: bytes,
    *,
    seal: str = "aes-gcm",
    now: float | None = None,
) -> dict[str, str]:
    """Headers for one signed request (a fresh nonce every call)."""
    ts = str(int(now if now is not None else time.time()))
    nonce = secrets.token_hex(16)
    sig = hmac.new(device_key, canonical(method, path, ts, nonce, body), hashlib.sha256).hexdigest()
    return {H_DEVICE: device_id, H_TS: ts, H_NONCE: nonce, H_SIG: sig, H_SEAL: seal}


def verify_signature(device_key: bytes, method: str, path: str, headers, body: bytes) -> bool:
    """Constant-time check of ``X-Navig-Sig`` (freshness and replay are the caller's)."""
    expected = hmac.new(
        device_key,
        canonical(method, path, headers.get(H_TS, ""), headers.get(H_NONCE, ""), body),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, headers.get(H_SIG, ""))


def _seal_key(device_key: bytes, nonce: str) -> bytes:
    from cryptography.hazmat.primitives import hashes  # noqa: PLC0415
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF  # noqa: PLC0415

    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=SEAL_INFO + nonce.encode("ascii")
    ).derive(device_key)


def _aad(label: str, version: int) -> bytes:
    return f"{label}\n{version}".encode()


def seal_value(device_key: bytes, nonce: str, label: str, version: int, plaintext: str) -> str:
    from ..crypto import CryptoEngine  # noqa: PLC0415

    blob = CryptoEngine.seal(_seal_key(device_key, nonce), plaintext.encode("utf-8"), aad=_aad(label, version))
    return base64.b64encode(blob).decode("ascii")


def open_value(device_key: bytes, nonce: str, label: str, version: int, sealed_b64: str) -> str:
    from ..crypto import CryptoEngine  # noqa: PLC0415

    blob = base64.b64decode(sealed_b64.encode("ascii"))
    return CryptoEngine.open(_seal_key(device_key, nonce), blob, aad=_aad(label, version)).decode("utf-8")
