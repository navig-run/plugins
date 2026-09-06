"""Shared types, endpoints, and errors for the eBay engine."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


class EbayError(Exception):
    """Base class for all navig-ebay engine errors."""


class EbayConfigError(EbayError):
    """Missing/invalid configuration (env, marketplace, RuName …)."""


class EbayAuthError(EbayError):
    """Authentication/authorization problem (no creds, no token, refresh failed)."""


class EbayApiError(EbayError):
    """A Sell/Browse API call returned an error.

    Carries the HTTP status and eBay's structured error payload so callers can
    surface the exact ``errorId``/``message`` eBay returned rather than a generic
    failure.
    """

    def __init__(self, status: int, message: str, payload: Any = None) -> None:
        super().__init__(f"[{status}] {message}")
        self.status = status
        self.message = message
        self.payload = payload


# ---------------------------------------------------------------------------
# Endpoints (per environment)
# ---------------------------------------------------------------------------

ENDPOINTS: dict[str, dict[str, str]] = {
    "sandbox": {
        "authorize": "https://auth.sandbox.ebay.com/oauth2/authorize",
        "token": "https://api.sandbox.ebay.com/identity/v1/oauth2/token",
        "api": "https://api.sandbox.ebay.com",
        "trading": "https://api.sandbox.ebay.com/ws/api.dll",
    },
    "production": {
        "authorize": "https://auth.ebay.com/oauth2/authorize",
        "token": "https://api.ebay.com/identity/v1/oauth2/token",
        "api": "https://api.ebay.com",
        "trading": "https://api.ebay.com/ws/api.dll",
    },
}


def endpoints_for(environment: str) -> dict[str, str]:
    env = (environment or "sandbox").lower()
    if env not in ENDPOINTS:
        raise EbayConfigError(
            f"unknown environment {environment!r}; expected 'sandbox' or 'production'"
        )
    return ENDPOINTS[env]


# OAuth scopes required for the full selling toolbox. buy.browse enables the
# pricing proxy via the Browse API.
USER_SCOPES: list[str] = [
    "https://api.ebay.com/oauth/api_scope",
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
    "https://api.ebay.com/oauth/api_scope/sell.account",
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment",
    "https://api.ebay.com/oauth/api_scope/sell.marketing",
    "https://api.ebay.com/oauth/api_scope/buy.browse",
]

# The client-credentials (application) token used for the Browse API.
APP_SCOPES: list[str] = ["https://api.ebay.com/oauth/api_scope"]


@dataclass
class TokenSet:
    """OAuth token bundle.

    Mirrors the shape of ``navig.providers.oauth.OAuthCredentials`` (access /
    refresh / expires-in-ms) so it round-trips through the navig vault as a plain
    dict, while staying importable without navig-core for unit tests.
    """

    access: str
    refresh: str = ""
    expires: int = 0  # unix epoch milliseconds
    account_id: str | None = None
    email: str | None = None
    client_id: str | None = None

    @property
    def is_expired(self) -> bool:
        # 5-minute safety buffer, matching navig's OAuthCredentials.
        buffer_ms = 5 * 60 * 1000
        return time.time() * 1000 >= self.expires - buffer_ms

    def to_dict(self) -> dict[str, Any]:
        return {
            "access": self.access,
            "refresh": self.refresh,
            "expires": self.expires,
            "account_id": self.account_id,
            "email": self.email,
            "client_id": self.client_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TokenSet":
        return cls(
            access=data.get("access", ""),
            refresh=data.get("refresh", ""),
            expires=int(data.get("expires", 0) or 0),
            account_id=data.get("account_id"),
            email=data.get("email"),
            client_id=data.get("client_id"),
        )
