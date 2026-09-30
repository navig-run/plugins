"""The connected Gmail account, as a hydrated connector.

Reuses the OAuth connector the rest of navig speaks (Deck, MCP tools, notify's email
channel) — the mailroom never carries its own credentials. ``--account`` is reserved:
today the vault holds one Gmail token (``profile_id="connector"``), so the connected
account is *the* account; the flag exists so scripts written now keep working when
multi-account lands.
"""

from __future__ import annotations

import asyncio
from typing import Any

from . import gmail

from navig_sdk.host import command_name  # noqa: E402

# The command a user types here: `navig email` inside navig, `navig-email` on its own.
CMD = command_name("email")


class NotConnected(RuntimeError):
    """No usable Gmail token in the vault."""


# The account every command in this process works on (set by `navig email --account`).
CURRENT: str | None = None


def use(account: str | None) -> None:
    global CURRENT
    CURRENT = (account or "").strip().lower() or None


def connected_email(account: str | None = None) -> str:
    acct = account if account is not None else CURRENT
    if not gmail.oauth_connected(acct):
        from navig_email import imap_account

        creds = imap_account.load()
        if creds and (acct is None or creds[0].lower() == acct.strip().lower()):
            return creds[0].lower()
    try:
        from navig.connectors.auth_manager import ConnectorAuthManager
        from navig.connectors.bootstrap import ensure_connectors_loaded

        ensure_connectors_loaded()
        auth = ConnectorAuthManager()
        acct = account if account is not None else CURRENT
        slot = auth.resolve_account("gmail", acct) if acct else None
        return (auth.get_connected_account("gmail", slot) or "").strip().lower()
    except Exception:  # noqa: BLE001 — no vault / no connector / unknown account = ""
        return ""


def linked_accounts() -> list[str]:
    out: list[str] = []
    try:
        from navig.connectors.auth_manager import ConnectorAuthManager
        from navig.connectors.bootstrap import ensure_connectors_loaded

        ensure_connectors_loaded()
        out = list(ConnectorAuthManager().list_accounts("gmail"))
    except Exception:  # noqa: BLE001 - no navig / no connector: only the IMAP account, if any
        pass
    from navig_email import imap_account

    creds = imap_account.load()
    if creds and creds[0].lower() not in [a.lower() for a in out]:
        out.append(creds[0].lower())
    return out


def is_connected(account: str | None = None) -> bool:
    return gmail.is_connected(account if account is not None else CURRENT)


async def connector(account: str | None = None):
    """A ``GmailConnector`` with a live token for *account* (or the current/default one),
    or raise ``NotConnected``."""
    from navig_email.errors import ConnectorAuthError

    acct = account if account is not None else CURRENT
    try:
        c = await gmail._connector(acct)
    except ConnectorAuthError as exc:
        raise NotConnected(str(exc)) from exc
    if c is None:
        linked = linked_accounts()
        from navig_sdk.host import command_name, navig_available

        how = (
            f"Run `navig connector connect gmail` (OAuth), or `{CMD} imap connect "
            "you@gmail.com` (Gmail app password)."
            if navig_available()
            else f"Run `{command_name('email')} imap connect you@gmail.com` with a Gmail app "
            "password (Google Account → Security → App passwords)."
        )
        raise NotConnected(
            "Gmail is not connected. " + how + (f" Linked: {', '.join(linked)}" if linked else "")
        )
    return c


async def profile(c) -> dict[str, Any]:
    data = await c.get_profile()
    return {
        "email": str(data.get("emailAddress", "") or ""),
        "history_id": str(data.get("historyId", "") or ""),
        "messages_total": int(data.get("messagesTotal") or 0),
        "threads_total": int(data.get("threadsTotal") or 0),
    }


def run(coro):
    """``asyncio.run`` for the CLI (one loop per command)."""
    return asyncio.run(coro)
