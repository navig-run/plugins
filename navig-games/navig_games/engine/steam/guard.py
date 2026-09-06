"""Steam Guard — generate the 5-char mobile-authenticator code from a
``shared_secret`` (the SteamDesktopAuthenticator / maFile concept, native).

The secret is stored in the NAVIG vault; the code is derived locally with the
canonical Steam TOTP algorithm (HMAC-SHA1 over time/30, mapped through Steam's
custom 26-char alphabet). This enables unattended Steam login in a later phase.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import struct
import time
from pathlib import Path

_log = logging.getLogger(__name__)

_ALPHABET = "23456789BCDFGHJKMNPQRTVWXY"  # Steam's code alphabet (no ambiguous chars)


def generate_code(shared_secret: str, timestamp: float | None = None) -> str:
    """Return the current 5-character Steam Guard code for a base64 shared_secret."""
    secret = base64.b64decode(shared_secret)
    t = int((time.time() if timestamp is None else timestamp) // 30)
    mac = hmac.new(secret, struct.pack(">Q", t), hashlib.sha1).digest()
    start = mac[19] & 0x0F
    code_int = struct.unpack_from(">I", mac, start)[0] & 0x7FFFFFFF
    out = []
    for _ in range(5):
        out.append(_ALPHABET[code_int % len(_ALPHABET)])
        code_int //= len(_ALPHABET)
    return "".join(out)


def seconds_remaining(timestamp: float | None = None) -> int:
    """Seconds until the current code rolls over (Steam codes last 30s)."""
    now = time.time() if timestamp is None else timestamp
    return 30 - int(now % 30)


def is_valid_secret(shared_secret: str) -> bool:
    try:
        return len(base64.b64decode(shared_secret)) == 20
    except Exception:  # noqa: BLE001
        return False


# ── vault storage ────────────────────────────────────────────────────────────

def _label(account: str) -> str:
    return f"steam_guard:{account}"


def store_secret(account: str, shared_secret: str, identity_secret: str = "") -> bool:
    """Persist a Steam Guard secret in the vault + record the account in config."""
    if not is_valid_secret(shared_secret):
        return False
    try:
        from navig.vault import get_vault

        v = get_vault()
        label = _label(account)
        data = {"shared_secret": shared_secret, "identity_secret": identity_secret,
                "account": account}
        existing = v.get(label, caller="navig-games.steam")
        if existing:
            v.update(existing.id, data=data)
        else:
            v.add(provider=label, credential_type="totp", data=data,
                  metadata={"description": f"Steam Guard for {account}"})
    except Exception as exc:  # noqa: BLE001
        _log.warning("games.steam: could not store secret (%s)", exc)
        return False
    _remember_account(account)
    return True


def get_secret(account: str) -> dict | None:
    try:
        from navig.vault import get_vault

        s = get_vault().get(_label(account), caller="navig-games.steam")
        if s is None:
            return None
        data = getattr(s, "data", None)
        if isinstance(data, dict):
            return data
    except Exception:  # noqa: BLE001
        pass
    return None


def list_accounts() -> list[str]:
    from .. import settings

    accts = settings.get("steam_accounts", [])
    return list(accts) if isinstance(accts, list) else []


def _remember_account(account: str) -> None:
    from .. import settings

    accts = list_accounts()
    if account not in accts:
        accts.append(account)
        settings.set("steam_accounts", accts)


def code_for(account: str) -> str | None:
    """Convenience: the current code for a stored account (None if unknown)."""
    data = get_secret(account)
    if not data or not data.get("shared_secret"):
        return None
    return generate_code(data["shared_secret"])


# ── maFile import ────────────────────────────────────────────────────────────

def import_mafile(path: Path) -> dict:
    """Import a SteamDesktopAuthenticator .maFile (JSON) into the vault.

    Returns {"ok", "account"} or {"ok": False, "error"}.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError) as exc:
        return {"ok": False, "error": f"could not read maFile: {exc}"}
    shared = data.get("shared_secret", "")
    identity = data.get("identity_secret", "")
    account = data.get("account_name") or (data.get("Session", {}) or {}).get("Username") or "steam"
    if not is_valid_secret(shared):
        return {"ok": False, "error": "maFile has no valid shared_secret"}
    if store_secret(account, shared, identity):
        return {"ok": True, "account": account}
    return {"ok": False, "error": "vault unavailable"}
