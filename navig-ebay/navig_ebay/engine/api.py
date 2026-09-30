"""Authenticated REST client for eBay's Sell & Browse APIs.

A thin ``requests.Session`` wrapper (mirroring navig-github's engine) that:
  * targets the correct base URL for the active environment,
  * injects the right bearer token (user token for Sell APIs, app token for
    Browse), refreshing on expiry via :class:`EbayAuth`,
  * sets marketplace / content-language headers,
  * retries idempotent calls with exponential backoff (navig's retry SSOT when
    available), and
  * raises :class:`EbayApiError` carrying eBay's structured error payload.
"""

from __future__ import annotations

import time
from typing import Any

import requests

from .models import EbayApiError, endpoints_for
from .oauth_ebay import EbayAuth

_TIMEOUT = 45
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3


def _sleep_for(attempt: int) -> float:
    """Backoff delay, preferring navig's retry policy SSOT."""
    try:
        from navig.retry_policy import BackoffPolicy  # type: ignore

        return BackoffPolicy().delay_s(attempt)
    except Exception:  # pragma: no cover — fallback
        return min(2.0 ** attempt, 8.0)


class EbayClient:
    """REST client bound to an :class:`EbayAuth` and its environment."""

    def __init__(self, auth: EbayAuth, *, token_kind: str = "user") -> None:
        self.auth = auth
        self.config = auth.config
        self.token_kind = token_kind
        self.base = endpoints_for(self.config.environment)["api"]
        self._session = requests.Session()

    def _bearer(self) -> str:
        if self.token_kind == "app":
            return self.auth.get_app_token()
        return self.auth.get_access_token()

    def _headers(self, extra: dict | None = None) -> dict:
        headers = {
            "Authorization": f"Bearer {self._bearer()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Content-Language": self.config.content_language,
            "X-EBAY-C-MARKETPLACE-ID": self.config.marketplace_id,
        }
        if extra:
            headers.update(extra)
        return headers

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict | None = None,
        headers: dict | None = None,
        allow: tuple[int, ...] = (200, 201, 204),
    ) -> Any:
        """Make an API call. ``path`` is relative to the environment base URL.

        Returns parsed JSON (or ``{}`` for empty 2xx bodies). Raises
        :class:`EbayApiError` on non-allowed statuses.
        """
        url = path if path.startswith("http") else f"{self.base}{path}"
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = self._session.request(
                    method.upper(),
                    url,
                    json=json,
                    params=params,
                    headers=self._headers(headers),
                    timeout=_TIMEOUT,
                )
            except requests.RequestException as exc:  # network error → retry
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    time.sleep(_sleep_for(attempt))
                    continue
                raise EbayApiError(0, f"network error calling eBay: {exc}") from exc

            if resp.status_code in _RETRY_STATUSES and attempt < _MAX_RETRIES:
                time.sleep(_sleep_for(attempt))
                continue

            if resp.status_code in allow:
                if resp.status_code == 204 or not resp.content:
                    return {}
                try:
                    return resp.json()
                except ValueError:
                    return {}

            # Error — surface eBay's structured payload.
            payload: Any
            try:
                payload = resp.json()
            except ValueError:
                payload = resp.text
            raise EbayApiError(resp.status_code, _describe_error(payload), payload)

        # Exhausted retries on retryable statuses.
        raise EbayApiError(0, f"eBay call failed after retries: {last_exc}")

    # convenience verbs
    def get(self, path: str, **kw: Any) -> Any:
        return self.request("GET", path, allow=(200,), **kw)

    def post(self, path: str, **kw: Any) -> Any:
        return self.request("POST", path, **kw)

    def put(self, path: str, **kw: Any) -> Any:
        return self.request("PUT", path, **kw)

    def delete(self, path: str, **kw: Any) -> Any:
        return self.request("DELETE", path, allow=(200, 204), **kw)


def _describe_error(payload: Any) -> str:
    """Extract a human-readable message from eBay's error envelope."""
    if isinstance(payload, dict):
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            parts = []
            for err in errors:
                if isinstance(err, dict):
                    msg = err.get("message") or err.get("longMessage") or ""
                    eid = err.get("errorId")
                    parts.append(f"{msg} (errorId {eid})" if eid else msg)
            if parts:
                return "; ".join(p for p in parts if p)
        if payload.get("message"):
            return str(payload["message"])
    return str(payload)[:500]
