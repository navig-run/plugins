"""Server-side Gmail read helpers for the email-ops service — reuse the native
OAuth connector (same pattern as navig.notify.email)."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("navig_email")


def oauth_connected(account: str | None = None) -> bool:
    """navig's OAuth Gmail connector is linked (always False without navig)."""
    try:
        from navig.connectors.auth_manager import ConnectorAuthManager
        from navig.connectors.bootstrap import ensure_connectors_loaded

        ensure_connectors_loaded()
        auth = ConnectorAuthManager()
        slot = auth.resolve_account("gmail", account) if account else None
        return auth.is_connected("gmail", slot)
    except Exception:
        return False


def is_connected(account: str | None = None) -> bool:
    """A Gmail the mailroom can use: navig's OAuth connector, or the IMAP app password."""
    if oauth_connected(account):
        return True
    from navig_email import imap_account

    creds = imap_account.load()
    return bool(creds and (account is None or creds[0].lower() == account.strip().lower()))


async def _connector(account: str | None = None):
    """A hydrated Gmail connector for *account* (email) or the default linked account.

    navig's OAuth ``GmailConnector`` when navig is installed and Gmail is linked there;
    otherwise the Gmail-over-IMAP backend (``navig_email.imap``) when an app password is
    stored. Both answer the same methods with the same raw-Gmail-JSON shapes.
    """
    if oauth_connected(account):
        return await _oauth_connector(account)
    from navig_email import imap_account

    creds = imap_account.load()
    if creds and (account is None or creds[0].lower() == account.strip().lower()):
        return imap_account.backend()
    return None


async def _oauth_connector(account: str | None = None):
    from navig.connectors.auth_manager import ConnectorAuthManager
    from navig.connectors.bootstrap import ensure_connectors_loaded
    from navig.connectors.registry import get_connector_registry

    ensure_connectors_loaded()
    auth = ConnectorAuthManager()
    slot = (
        auth.resolve_account("gmail", account) if account else None
    )  # raises when unknown
    if not auth.is_connected("gmail", slot):
        return None
    obj = get_connector_registry().get("gmail")
    connector = obj() if isinstance(obj, type) else obj
    if connector is None:
        return None
    if not await auth.inject_token(connector, slot):
        return None
    return connector


def _shape(r) -> dict[str, Any]:
    md = getattr(r, "metadata", None) or {}
    return {
        "id": r.id,
        "subject": r.title or "",
        "from": md.get("from", ""),
        "to": md.get("to", ""),
        "snippet": r.preview or "",
        "body": md.get("body", "") or r.preview or "",
        "url": getattr(r, "url", "") or "",
        "date": getattr(r, "timestamp", "") or "",
        "labels": md.get("labels", []),
    }


async def search(query: str, limit: int = 15) -> list[dict[str, Any]]:
    """Search Gmail (metadata only — subject/from/snippet). Empty on any error."""
    connector = await _connector()
    if connector is None:
        return []
    try:
        results = await connector.search(query or "newer_than:1d", limit=limit)
        return [_shape(r) for r in results]
    except Exception:
        logger.debug("email_ops gmail search failed", exc_info=True)
        return []


async def fetch_body(resource_id: str) -> str:
    connector = await _connector()
    if connector is None:
        return ""
    try:
        r = await connector.fetch(resource_id)
        return (getattr(r, "metadata", None) or {}).get("body", "") if r else ""
    except Exception:
        logger.debug("email_ops gmail fetch failed", exc_info=True)
        return ""
