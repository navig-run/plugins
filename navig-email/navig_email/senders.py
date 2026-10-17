"""Which ``From`` addresses an account may send as — the alias half of multi-account isolation.

One Gmail mailbox receives mail for many addresses (``support@cybesis.com``, ``hello@…``), and
``mailroom/accounts.yaml`` is the single place that says which aliases belong to which mailbox.
A reply is only ever sent from the mailbox's own address or one of ITS aliases; an address that
belongs to a different account — or to nobody — is refused at enqueue and again at dispatch.
"""

from __future__ import annotations

from email.utils import getaddresses, parseaddr
from typing import Any

from navig_email import edge as E


def _norm(addr: str) -> str:
    return parseaddr(addr or "")[1].strip().lower()


def account_entries(paths: Any) -> list[dict[str, Any]]:
    if paths is None:
        return []
    data = E.read_accounts(paths)
    return [a for a in (data.get("accounts") or []) if isinstance(a, dict)]


def aliases_for(paths: Any, account: str) -> list[str]:
    """Alias addresses (lowercased) declared for *account* in accounts.yaml."""
    acct = _norm(account)
    out: list[str] = []
    for entry in account_entries(paths):
        if _norm(str(entry.get("address") or "")) != acct:
            continue
        for alias in entry.get("aliases") or []:
            addr = alias.get("address") if isinstance(alias, dict) else alias
            a = _norm(str(addr or ""))
            if a and a not in out:
                out.append(a)
    return out


def authorized_senders(paths: Any, account: str) -> list[str]:
    acct = _norm(account)
    return ([acct] if acct else []) + [a for a in aliases_for(paths, account) if a != acct]


def is_authorized(paths: Any, account: str, from_addr: str) -> bool:
    return bool(_norm(from_addr)) and _norm(from_addr) in authorized_senders(paths, account)


def owner_of(paths: Any, address: str) -> str:
    """The account whose mailbox (or alias list) holds *address*; ``""`` when none does."""
    addr = _norm(address)
    for entry in account_entries(paths):
        acct = _norm(str(entry.get("address") or ""))
        if addr == acct or addr in aliases_for(paths, acct):
            return acct
    return ""


def pick_sender(paths: Any, account: str, *, alias_hint: str = "", to_header: str = "") -> str:
    """The address a reply goes out from: the alias the mail was addressed to when it is one
    of this account's, else the account itself. Never an address of another account."""
    allowed = authorized_senders(paths, account)
    if alias_hint:
        local = alias_hint.strip().lower().split("@")[0]
        for a in allowed:
            if a.split("@")[0] == local and a != _norm(account):
                return a
    for _name, addr in getaddresses([to_header or ""]):
        a = addr.strip().lower()
        if a in allowed:
            return a
    return _norm(account)
