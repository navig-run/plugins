"""The Gmail app password the IMAP backend signs in with — kept in the encrypted navig vault.

Stored as the vault credential ``gmail_imap`` (``{"user", "password"}``) through the standalone
``navig-vault`` package: the same encrypted vault navig itself reads, so a password saved with
``navig-email imap connect`` is the one ``navig email`` uses too. For scripts and CI the pair
can come from ``NAVIG_EMAIL_IMAP_USER`` / ``NAVIG_EMAIL_IMAP_PASSWORD`` instead, which win.

Never logs or prints the password.
"""

from __future__ import annotations

import os
from typing import Any

PROVIDER = "gmail_imap"
ENV_USER, ENV_PASSWORD = "NAVIG_EMAIL_IMAP_USER", "NAVIG_EMAIL_IMAP_PASSWORD"


def _vault() -> Any:
    from navig_vault.core import get_vault

    return get_vault()


def load() -> tuple[str, str] | None:
    """``(user, app_password)`` from the environment, else the vault; ``None`` when neither."""
    # Google shows an app password in four groups ("abcd efgh ijkl mnop"); the spaces are
    # display only, so both sources are normalised the same way.
    user, password = os.environ.get(ENV_USER, "").strip(), os.environ.get(ENV_PASSWORD, "").replace(" ", "").strip()
    if user and password:
        return user, password
    try:
        cred = _vault().get(PROVIDER, caller="navig-email.imap")
    except Exception:  # noqa: BLE001 - no vault / locked vault = not configured, never a crash
        return None
    data = getattr(cred, "data", None) or {}
    user, password = str(data.get("user", "")).strip(), str(data.get("password", "")).replace(" ", "").strip()
    return (user, password) if user and password else None


def source() -> str:
    """Where the credentials come from: ``env`` | ``vault`` | ``""``."""
    if os.environ.get(ENV_USER, "").strip() and os.environ.get(ENV_PASSWORD, "").strip():
        return "env"
    return "vault" if load() else ""


def save(user: str, password: str) -> None:
    """Store (or replace) the app password in the vault."""
    vault = _vault()
    data = {"user": user.strip(), "password": password.strip().replace(" ", "")}
    existing = vault.get(PROVIDER, caller="navig-email.imap")
    if existing is not None:
        vault.update(existing.id, data=data)
    else:
        vault.add(provider=PROVIDER, credential_type="email", data=data,
                  metadata={"description": "Gmail app password for navig-email's IMAP mode"})


def remove() -> bool:
    vault = _vault()
    existing = vault.get(PROVIDER, caller="navig-email.imap")
    return bool(existing is not None and vault.delete(existing.id))


def backend() -> Any:
    """A ``GmailImap`` for the stored credentials, or ``None`` when none are configured."""
    creds = load()
    if not creds:
        return None
    from navig_email.imap import GmailImap

    return GmailImap(*creds)
