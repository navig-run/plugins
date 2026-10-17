"""Several accounts per network — a second YouTube channel beside the first.

Connecting a network wrote its token into the ``<provider>`` slot, so connecting a second
channel silently replaced the first. Now the first account keeps that slot, and its config
keys, exactly as before. Existing installs read unchanged, and code that knows nothing of
accounts still gets the default. Another account is stored beside it:

    token          <provider>                      (default)   <provider>@<id>
    refresh token  <provider>/refresh_token        (default)   <provider>@<id>/refresh_token
    settings       adapters.social.<p>.<key>       (default)   adapters.social.<p>.accounts[i].config

``adapters.social.<p>.account_id`` / ``account_label`` name the default account; ``accounts``
lists the others as ``{"id", "label", "config"}``. Reconnecting an account that is already
linked refreshes it in place.

Publishing picks an account through the post target: ``"@<id>"`` means "as account <id>".
A plain target keeps its old meaning (a page id, an author URN, …) on the default account.
"""

from __future__ import annotations

from typing import Any

ACCOUNTS_KEY = "accounts"
DEFAULT_ID_KEY = "account_id"
DEFAULT_LABEL_KEY = "account_label"


def _oauth():
    from navig_social.social import oauth

    return oauth


def _get_config(provider: str, key: str) -> Any:
    from navig_social.social.credentials import get_config

    return get_config(provider, key)


def token_label(provider: str, account: str | None = None) -> str:
    return f"{provider}@{account}" if account else provider


def refresh_label(provider: str, account: str | None = None) -> str:
    return f"{token_label(provider, account)}/refresh_token"


def extras(provider: str) -> list[dict[str, Any]]:
    """The non-default accounts, as stored (``{"id", "label", "config"}``)."""
    raw = _get_config(provider, ACCOUNTS_KEY)
    if not isinstance(raw, list):
        return []
    return [a for a in raw if isinstance(a, dict) and str(a.get("id") or "").strip()]


def default_id(provider: str) -> str:
    return str(_get_config(provider, DEFAULT_ID_KEY) or "").strip()


def is_default(provider: str, account: str | None) -> bool:
    return not account or account == default_id(provider)


def list_accounts(provider: str) -> list[dict[str, Any]]:
    """Every connected account — the default first: ``{"id", "label", "default"}``.

    The default's id is "" on an install connected before accounts were recorded.
    """
    out: list[dict[str, Any]] = []
    if _oauth().read_secret(provider):
        out.append({
            "id": default_id(provider),
            "label": str(_get_config(provider, DEFAULT_LABEL_KEY) or ""),
            "default": True,
        })
    for a in extras(provider):
        if _oauth().read_secret(token_label(provider, a["id"])):
            out.append({"id": str(a["id"]), "label": str(a.get("label") or a["id"]), "default": False})
    return out


def resolve_account(provider: str, selector: str | None) -> str | None:
    """Resolve an account id or unique display label to its stored id.

    ``None`` keeps the default-account behaviour.  A non-empty selector never
    falls back to the default: callers that are about to publish must fail closed
    rather than send through a different account.
    """
    wanted = (selector or "").strip().lstrip("@")
    if not wanted:
        return None
    linked = list_accounts(provider)
    for account in linked:
        if account["id"] == wanted:
            return str(account["id"])
    by_label = [a for a in linked if str(a.get("label") or "").casefold() == wanted.casefold()]
    if len(by_label) == 1:
        return str(by_label[0]["id"])
    if len(by_label) > 1:
        raise ValueError(f"several {provider} accounts are named {selector!r}; use the channel id")
    available = ", ".join(
        f"{a.get('label') or 'unnamed'} ({a.get('id') or 'legacy default'})" for a in linked
    ) or "none"
    raise ValueError(f"{provider} account {selector!r} is not connected; available: {available}")


def account_setting(provider: str, key: str, account: str | None = None) -> Any:
    """A per-account setting (a page id, an author URN); the default's lives at the top level."""
    if is_default(provider, account):
        return _get_config(provider, key)
    for a in extras(provider):
        if a["id"] == account:
            return (a.get("config") or {}).get(key)
    return None


def _write_extras(provider: str, accounts: list[dict[str, Any]]) -> None:
    _oauth().set_config(provider, ACCOUNTS_KEY, accounts)


def save_account(
    provider: str,
    account_id: str,
    label: str,
    token: str,
    *,
    token_title: str = "",
    refresh: str | None = None,
    config: dict[str, Any] | None = None,
) -> str:
    """Store a freshly connected account; returns "default" or the account id it went to.

    The default slot takes it when the network has no account yet, when it IS the default
    account (a reconnect), or on an install whose default predates recorded ids (the old
    behaviour, so a reconnect there still refreshes rather than duplicating).
    """
    oauth = _oauth()
    account_id = str(account_id).strip()
    current = default_id(provider)
    has_default = bool(oauth.read_secret(provider))
    if not has_default or not current or current == account_id:
        oauth.write_provider_token(provider, token, token_title or f"{provider} token ({label})")
        if refresh:
            oauth.write_path_secret(refresh_label(provider), refresh)
        oauth.set_config(provider, DEFAULT_ID_KEY, account_id)
        oauth.set_config(provider, DEFAULT_LABEL_KEY, label)
        for key, value in (config or {}).items():
            oauth.set_config(provider, key, value)
        others = [a for a in extras(provider) if a["id"] != account_id]
        if len(others) != len(extras(provider)):
            _write_extras(provider, others)
        return "default"

    oauth.write_path_secret(token_label(provider, account_id), token)
    if refresh:
        oauth.write_path_secret(refresh_label(provider, account_id), refresh)
    others = [a for a in extras(provider) if a["id"] != account_id]
    _write_extras(provider, [*others, {"id": account_id, "label": label, "config": dict(config or {})}])
    return account_id


def store_token(provider: str, account: str | None, token: str, title: str) -> None:
    """Write a renewed access token back into the slot it came from."""
    oauth = _oauth()
    if is_default(provider, account):
        oauth.write_provider_token(provider, token, title)
    else:
        oauth.write_path_secret(token_label(provider, account), token)


def remove_account(provider: str, account: str | None = None) -> bool:
    """Disconnect one account (the default when None); True when a token was removed.

    Removing the default moves the next account into its place, settings included, so the
    network stays connected and code that only knows the default keeps working.
    """
    oauth = _oauth()
    if not is_default(provider, account):
        removed = oauth.delete_secret(token_label(provider, account))
        oauth.delete_secret(refresh_label(provider, account))
        before = extras(provider)
        after = [a for a in before if a["id"] != account]
        if len(after) != len(before):
            _write_extras(provider, after)
            removed = True
        return removed

    removed = oauth.delete_secret(provider)
    oauth.delete_secret(refresh_label(provider))
    rest = extras(provider)
    if not rest:
        oauth.set_config(provider, DEFAULT_ID_KEY, "")
        oauth.set_config(provider, DEFAULT_LABEL_KEY, "")
        return removed
    nxt, rest = rest[0], rest[1:]
    token = oauth.read_secret(token_label(provider, nxt["id"]))
    refresh = oauth.read_secret(refresh_label(provider, nxt["id"]))
    if token:
        oauth.write_provider_token(provider, token, f"{provider} token ({nxt.get('label') or nxt['id']})")
        if refresh:
            oauth.write_path_secret(refresh_label(provider), refresh)
    oauth.delete_secret(token_label(provider, nxt["id"]))
    oauth.delete_secret(refresh_label(provider, nxt["id"]))
    oauth.set_config(provider, DEFAULT_ID_KEY, str(nxt["id"]))
    oauth.set_config(provider, DEFAULT_LABEL_KEY, str(nxt.get("label") or ""))
    for key, value in (nxt.get("config") or {}).items():
        oauth.set_config(provider, key, value)
    _write_extras(provider, rest)
    return removed


def split_target(target: str) -> tuple[str | None, str]:
    """``"@<id>"`` → (id, "") — publish as that account; anything else → (None, target)."""
    target = (target or "").strip()
    if target.startswith("@") and len(target) > 1:
        return target[1:], ""
    return None, target
