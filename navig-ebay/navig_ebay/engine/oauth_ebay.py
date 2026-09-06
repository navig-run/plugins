"""eBay OAuth for navig-ebay.

eBay deviates from navig's generic OAuth engine in two ways, so this module
implements the token calls directly (while storing tokens in the navig vault in a
shape compatible with ``navig.providers.oauth.OAuthCredentials``):

  1. The token endpoint requires **HTTP Basic auth** (``client_id:client_secret``),
     not a client_secret in the body, and does **not** use PKCE.
  2. The redirect must be a registered **RuName** (an HTTPS redirect alias), not a
     localhost callback — so login is browser-consent → paste-the-code.

Credential layout in the vault (provider ``ebay``):
  * profile ``app``       — {client_id, client_secret, ru_name}
  * profile ``oauth``     — user TokenSet (access + refresh)
  * profile ``app_token`` — cached client-credentials app token (Browse API)
"""

from __future__ import annotations

import os
import time
from urllib.parse import urlencode

import requests

from .config import EbayConfig, EbayConfigManager
from .models import (
    APP_SCOPES,
    USER_SCOPES,
    EbayAuthError,
    TokenSet,
    endpoints_for,
)

_TOKEN_TIMEOUT = 30


# ---------------------------------------------------------------------------
# Low-level token calls
# ---------------------------------------------------------------------------
def build_authorize_url(
    *, client_id: str, ru_name: str, environment: str, state: str, scopes: list[str] | None = None
) -> str:
    """Build the eBay consent URL the user opens in a browser."""
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": ru_name,
        "scope": " ".join(scopes or USER_SCOPES),
        "state": state,
    }
    return f"{endpoints_for(environment)['authorize']}?{urlencode(params)}"


def _post_token(environment: str, client_id: str, client_secret: str, data: dict) -> dict:
    url = endpoints_for(environment)["token"]
    resp = requests.post(
        url,
        data=data,
        auth=(client_id, client_secret),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=_TOKEN_TIMEOUT,
    )
    if resp.status_code != 200:
        raise EbayAuthError(f"eBay token endpoint returned {resp.status_code}: {resp.text}")
    return resp.json()


def exchange_code(
    *, environment: str, client_id: str, client_secret: str, code: str, ru_name: str
) -> TokenSet:
    """Exchange an authorization code (from the consent redirect) for user tokens."""
    body = _post_token(
        environment,
        client_id,
        client_secret,
        {"grant_type": "authorization_code", "code": code, "redirect_uri": ru_name},
    )
    return _token_from_response(body, client_id)


def refresh_user_token(
    *, environment: str, client_id: str, client_secret: str, refresh_token: str
) -> TokenSet:
    """Mint a fresh access token from the long-lived refresh token."""
    if not refresh_token:
        raise EbayAuthError("no refresh token stored; run `navig ebay auth login`")
    body = _post_token(
        environment,
        client_id,
        client_secret,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": " ".join(USER_SCOPES),
        },
    )
    ts = _token_from_response(body, client_id)
    # eBay does not return a new refresh token on refresh — keep the existing one.
    if not ts.refresh:
        ts.refresh = refresh_token
    return ts


def fetch_app_token(*, environment: str, client_id: str, client_secret: str) -> TokenSet:
    """Client-credentials (application) token, used for the Browse API."""
    body = _post_token(
        environment,
        client_id,
        client_secret,
        {"grant_type": "client_credentials", "scope": " ".join(APP_SCOPES)},
    )
    return _token_from_response(body, client_id)


def _token_from_response(body: dict, client_id: str) -> TokenSet:
    expires_in = int(body.get("expires_in", 7200) or 7200)
    return TokenSet(
        access=body.get("access_token", ""),
        refresh=body.get("refresh_token", ""),
        expires=int((time.time() + expires_in) * 1000),
        client_id=client_id,
    )


# ---------------------------------------------------------------------------
# Vault-backed auth manager
# ---------------------------------------------------------------------------
def _get_vault():
    try:
        from navig.vault import get_vault  # type: ignore

        return get_vault()
    except Exception as exc:  # pragma: no cover — vault unavailable
        raise EbayAuthError(f"navig vault unavailable: {exc}") from exc


class EbayAuth:
    """Ties the navig vault + config together for eBay credentials & tokens."""

    PROVIDER = "ebay"

    def __init__(self, config: EbayConfig | None = None, cfg_mgr: EbayConfigManager | None = None):
        self.cfg_mgr = cfg_mgr or EbayConfigManager()
        self.config = config or self.cfg_mgr.load()

    # -- app credentials --------------------------------------------------
    def store_app_credentials(self, client_id: str, client_secret: str, ru_name: str) -> None:
        vault = _get_vault()
        data = {"client_id": client_id, "client_secret": client_secret, "ru_name": ru_name}
        self._upsert(vault, profile_id="app", credential_type="api_key", data=data,
                     label="eBay app credentials")
        # ru_name is non-secret and needed to build the authorize URL — mirror it into config.
        self.config = self.cfg_mgr.update(ru_name=ru_name)

    def load_app_credentials(self) -> tuple[str, str, str]:
        vault = _get_vault()
        cred = vault.get(self.PROVIDER, profile_id="app", caller="navig-ebay")
        if cred is None or not getattr(cred, "data", None):
            raise EbayAuthError("no eBay app credentials stored; run `navig ebay creds set`")
        data = cred.data
        cid = data.get("client_id")
        secret = data.get("client_secret")
        ru = data.get("ru_name") or self.config.ru_name
        if not cid or not secret:
            raise EbayAuthError("stored eBay app credentials are incomplete; re-run `creds set`")
        if not ru:
            raise EbayAuthError("no RuName stored; re-run `navig ebay creds set`")
        return cid, secret, ru

    def has_app_credentials(self) -> bool:
        try:
            self.load_app_credentials()
            return True
        except EbayAuthError:
            return False

    # -- user tokens ------------------------------------------------------
    def store_tokens(self, tokens: TokenSet) -> None:
        vault = _get_vault()
        self._upsert(
            vault,
            profile_id="oauth",
            credential_type="oauth",
            data=tokens.to_dict(),
            label="eBay OAuth",
            metadata={
                "marketplace": self.config.marketplace_id,
                "environment": self.config.environment,
            },
        )

    def load_tokens(self) -> TokenSet | None:
        vault = _get_vault()
        cred = vault.get(self.PROVIDER, profile_id="oauth", caller="navig-ebay")
        if cred is None or not getattr(cred, "data", None):
            return None
        return TokenSet.from_dict(cred.data)

    def is_connected(self) -> bool:
        tokens = self.load_tokens()
        # A refreshable token counts as connected even if the access token expired.
        return bool(tokens and tokens.refresh)

    def logout(self) -> bool:
        vault = _get_vault()
        cred = vault.get(self.PROVIDER, profile_id="oauth", caller="navig-ebay")
        if cred is None:
            return False
        return bool(vault.delete(cred.id))

    def get_access_token(self) -> str:
        """Return a valid user access token, refreshing on expiry.

        Resolution: EBAY_ACCESS_TOKEN env → vault (refresh if expired) → error.
        """
        env_tok = os.environ.get("EBAY_ACCESS_TOKEN", "").strip()
        if env_tok:
            return env_tok
        tokens = self.load_tokens()
        if tokens is None:
            raise EbayAuthError("not authenticated; run `navig ebay auth login`")
        if tokens.access and not tokens.is_expired:
            return tokens.access
        # Refresh.
        cid, secret, _ru = self.load_app_credentials()
        refreshed = refresh_user_token(
            environment=self.config.environment,
            client_id=cid,
            client_secret=secret,
            refresh_token=tokens.refresh,
        )
        self.store_tokens(refreshed)
        return refreshed.access

    def get_app_token(self) -> str:
        """Return a valid client-credentials app token (Browse API), cached in vault."""
        vault = _get_vault()
        cred = vault.get(self.PROVIDER, profile_id="app_token", caller="navig-ebay")
        if cred and getattr(cred, "data", None):
            ts = TokenSet.from_dict(cred.data)
            if ts.access and not ts.is_expired:
                return ts.access
        cid, secret, _ru = self.load_app_credentials()
        ts = fetch_app_token(
            environment=self.config.environment, client_id=cid, client_secret=secret
        )
        self._upsert(vault, profile_id="app_token", credential_type="token",
                     data=ts.to_dict(), label="eBay app token")
        return ts.access

    # -- helpers ----------------------------------------------------------
    def _upsert(self, vault, *, profile_id: str, credential_type: str, data: dict,
                label: str, metadata: dict | None = None) -> None:
        """Store-or-update in place (never remove-then-add — preserves the id and
        avoids dropping the refresh token on a failed write)."""
        existing = vault.get(self.PROVIDER, profile_id=profile_id, caller="navig-ebay.upsert")
        if existing is not None:
            vault.update(existing.id, data=data, metadata=metadata)
        else:
            vault.add(
                provider=self.PROVIDER,
                credential_type=credential_type,
                data=data,
                profile_id=profile_id,
                label=label,
                metadata=metadata,
            )
