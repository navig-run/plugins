"""HTTP repeater — send / edit / resend a request and read the full response.

The dev-tool half of a Burp/Postman "repeater": craft a request with any method,
headers and body, fire it, and get the status, headers, timing and body back. It
runs over httpx and reuses navig-sdk's SSRF guard, so every hop — the initial URL
and each redirect — is re-validated before any network I/O (a 302 to the cloud
metadata address is blocked, not followed). Private/internal targets are refused
unless the caller opts in (``allow_private``), mirroring
``net.ssrf.allow_private_network``.

Standalone-clean: depends only on httpx + navig-sdk, never navig.
"""

from __future__ import annotations

import time
import urllib.parse
from typing import Any

from navig_sdk.ssrf import SsrfPolicy, check_url, policy_from_config

DEFAULT_TIMEOUT = 15.0
#: Response body kept in the result (full length is always recorded separately).
BODY_PREVIEW_CAP = 256 * 1024
SAFE_METHODS = ("GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE")


def _resolve_policy(allow_private: bool) -> SsrfPolicy:
    if allow_private:
        return SsrfPolicy(allow_private_network=True)
    return policy_from_config()  # respects net.ssrf.* config; private blocked by default


def _header_list(headers: dict[str, str] | None) -> list[list[str]]:
    return [[k, v] for k, v in (headers or {}).items()]


async def send(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: str | bytes | None = None,
    allow_private: bool = False,
    follow_redirects: bool = True,
    max_redirects: int = 5,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Send one request and return a structured result.

    Returns ``{request, response, redirects, elapsed_ms}`` where ``response``
    holds ``status``, ``reason``, ``headers``, ``body`` (capped), ``body_len``,
    ``content_type`` and ``final_url``. Raises ``SsrfBlockedError`` for a blocked
    target/redirect and ``httpx`` errors for transport failures — callers surface
    them (the CLI and the agent tool both catch).
    """
    try:
        import httpx  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise ImportError("navig devnet http needs httpx (pip install httpx)") from exc

    method = (method or "GET").strip().upper()
    if method not in SAFE_METHODS:
        raise ValueError(f"unsupported method {method!r} (use one of {', '.join(SAFE_METHODS)})")

    policy = _resolve_policy(allow_private)
    check_url(url, policy)  # validate the initial target before any I/O

    content = body.encode() if isinstance(body, str) else body
    redirects: list[dict[str, Any]] = []
    cur_method, cur_url, cur_content = method, url, content
    t0 = time.monotonic()

    async with httpx.AsyncClient(follow_redirects=False) as client:
        for _ in range(max_redirects + 1):
            resp = await client.request(
                cur_method, cur_url,
                headers=headers, content=cur_content,
                timeout=httpx.Timeout(timeout),
            )
            location = resp.headers.get("location")
            if follow_redirects and resp.is_redirect and location:
                nxt = str(urllib.parse.urljoin(cur_url, location))
                check_url(nxt, policy)  # re-validate EVERY redirect target
                redirects.append({"from": cur_url, "to": nxt, "status": resp.status_code})
                # 303, and the common 301/302 behaviour for a body-carrying method,
                # drop to GET without a body; 307/308 preserve method and body.
                if resp.status_code in (301, 302, 303) and cur_method not in ("GET", "HEAD"):
                    cur_method, cur_content = "GET", None
                cur_url = nxt
                continue
            elapsed_ms = (time.monotonic() - t0) * 1000
            raw = resp.content or b""
            try:
                text = resp.text
            except Exception:  # noqa: BLE001 — binary/undecodable body
                text = ""
            return {
                "request": {
                    "method": method, "url": url,
                    "headers": _header_list(headers),
                    "body_len": len(content) if content else 0,
                },
                "response": {
                    "status": resp.status_code,
                    "reason": resp.reason_phrase,
                    "headers": [[k, v] for k, v in resp.headers.items()],
                    "content_type": resp.headers.get("content-type", ""),
                    "body": text[:BODY_PREVIEW_CAP],
                    "body_len": len(raw),
                    "body_truncated": len(text) > BODY_PREVIEW_CAP,
                    "final_url": str(resp.url),
                },
                "redirects": redirects,
                "elapsed_ms": round(elapsed_ms, 1),
            }
    raise ValueError(f"redirect chain exceeded {max_redirects} hops starting at {url!r}")


def send_sync(method: str, url: str, **kwargs) -> dict[str, Any]:
    """Blocking wrapper around :func:`send` for the CLI."""
    import asyncio

    return asyncio.run(send(method, url, **kwargs))
