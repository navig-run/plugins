"""Dead-link and redirect checks: HEAD first, never the page body, never a private address.

Fetch strategy adapted from linkding's ``services/website_loader.py`` (MIT, see
THIRD_PARTY_NOTICES.md): ask for headers only (``HEAD``); a server that refuses HEAD (405,
501, some 400/403) gets a ``GET`` whose body is never read. Redirects are followed HERE, one
hop at a time, and every hop is checked against navig-sdk's SSRF policy
(:mod:`navig_sdk.ssrf` — the same rules navig itself uses, including its
``net.ssrf.allow_private_network`` setting) before it is requested, so a public URL that
redirects to ``169.254.169.254`` or ``localhost`` is reported as blocked, never fetched.

Known limit, shared with navig's own guard: the hostname is resolved once for the check and
again by the HTTP client, so an actively hostile DNS server could still rebind between the
two. A proxy configured in the environment also bypasses the address check.

Verdicts: ``ok`` (2xx), ``redirect`` (ends somewhere else, usually fine — the new address is
recorded), ``restricted`` (401/403/429: alive but not for us), ``dead`` (404/410, or the host
no longer exists), ``error`` (5xx, timeouts, TLS trouble — maybe temporary), ``blocked``
(SSRF policy), ``skipped`` (not http/https).
"""

from __future__ import annotations

import json
import socket
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from navig_bookmarks.urls import normalize_url

USER_AGENT = "Mozilla/5.0 (compatible; navig-bookmarks link check; +https://navig.run)"
MAX_REDIRECTS = 5
WAYBACK_API = "https://archive.org/wayback/available?url="


@dataclass
class CheckResult:
    id: str
    url: str
    verdict: str
    status: int | None = None
    final_url: str | None = None
    error: str | None = None
    wayback: str | None = None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401, ANN001
        return None  # surface every 3xx to us as an HTTPError, so each hop is SSRF-checked


_OPENER = urllib.request.build_opener(_NoRedirect)


def _policy():
    from navig_sdk.ssrf import policy_from_config  # noqa: PLC0415

    return policy_from_config()


def _request(url: str, method: str, timeout: float) -> tuple[int, str | None]:
    """One request, no redirects followed: ``(status, location header)``."""
    req = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    if method == "GET":
        req.add_header("Range", "bytes=0-0")
    try:
        with _OPENER.open(req, timeout=timeout) as resp:  # noqa: S310 - scheme and address checked by the caller
            return resp.status, resp.headers.get("Location")
    except urllib.error.HTTPError as exc:
        location = exc.headers.get("Location") if exc.headers else None
        exc.close()
        return exc.code, location


def check_one(bookmark_id: str, url: str, *, timeout: float = 10.0, policy=None) -> CheckResult:
    from navig_sdk.ssrf import SsrfBlockedError, check_url  # noqa: PLC0415

    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme not in ("http", "https"):
        return CheckResult(bookmark_id, url, "skipped", error=f"{scheme or 'no'} scheme")
    policy = policy if policy is not None else _policy()
    current = url
    try:
        for _hop in range(MAX_REDIRECTS + 1):
            check_url(current, policy)
            status, location = _request(current, "HEAD", timeout)
            if status in (400, 403, 405, 501):
                status, location = _request(current, "GET", timeout)
            if 300 <= status < 400 and location:
                current = urllib.parse.urljoin(current, location)
                continue
            return _verdict(bookmark_id, url, current, status)
        return CheckResult(bookmark_id, url, "error", final_url=current, error=f"more than {MAX_REDIRECTS} redirects")
    except SsrfBlockedError as exc:
        return CheckResult(bookmark_id, url, "blocked", final_url=current if current != url else None,
                           error=f"private or reserved address{f' ({exc.resolved_ip})' if exc.resolved_ip else ''}")
    except ValueError as exc:
        return CheckResult(bookmark_id, url, "skipped", error=str(exc))
    except socket.gaierror:
        return CheckResult(bookmark_id, url, "dead", final_url=current if current != url else None,
                           error="host not found")
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, socket.gaierror):
            return CheckResult(bookmark_id, url, "dead", error="host not found")
        if isinstance(reason, ssl.SSLError):
            return CheckResult(bookmark_id, url, "error", error=f"TLS: {reason.__class__.__name__}")
        return CheckResult(bookmark_id, url, "error", error=str(reason)[:120])
    except (TimeoutError, socket.timeout):
        return CheckResult(bookmark_id, url, "error", error="timed out")
    except (OSError, ssl.SSLError) as exc:
        return CheckResult(bookmark_id, url, "error", error=f"{exc.__class__.__name__}: {exc}"[:120])


def _verdict(bookmark_id: str, url: str, final: str, status: int) -> CheckResult:
    moved = normalize_url(final) != normalize_url(url)
    final_url = final if moved else None
    if status in (404, 410):
        verdict = "dead"
    elif status in (401, 403, 429):
        verdict = "restricted"
    elif status >= 500:
        verdict = "error"
    elif moved:
        verdict = "redirect"
    else:
        verdict = "ok"
    return CheckResult(bookmark_id, url, verdict, status=status, final_url=final_url)


def wayback_snapshot(url: str, *, timeout: float = 10.0) -> str | None:
    """The closest archived copy on the Wayback Machine, or ``None``. Never raises."""
    try:
        req = urllib.request.Request(WAYBACK_API + urllib.parse.quote(url, safe=""), headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed public https host
            data = json.load(resp)
        snap = (data.get("archived_snapshots") or {}).get("closest") or {}
        if snap.get("available") and str(snap.get("status", "200")).startswith("2"):
            return str(snap.get("url") or "") or None
    except (OSError, ValueError):
        return None
    return None


def check_many(
    items: Iterable[tuple[str, str]],
    *,
    concurrency: int = 8,
    per_host: int = 2,
    timeout: float = 10.0,
    wayback: bool = True,
    on_result: Callable[[CheckResult], None] | None = None,
    checker: Callable[..., CheckResult] = check_one,
) -> list[CheckResult]:
    """Check ``(id, url)`` pairs, at most *concurrency* at once and *per_host* per host."""
    policy = _policy()
    host_locks: dict[str, threading.Semaphore] = defaultdict(lambda: threading.Semaphore(max(1, per_host)))
    guard = threading.Lock()

    def _run(bid: str, url: str) -> CheckResult:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        with guard:
            sem = host_locks[host]
        with sem:
            res = checker(bid, url, timeout=timeout, policy=policy)
        if wayback and res.verdict == "dead":
            res.wayback = wayback_snapshot(url, timeout=timeout)
        return res

    results: list[CheckResult] = []
    pairs = list(items)
    with ThreadPoolExecutor(max_workers=max(1, min(concurrency, 32))) as pool:
        futures = [pool.submit(_run, bid, url) for bid, url in pairs]
        for fut in as_completed(futures):
            res = fut.result()
            results.append(res)
            if on_result:
                on_result(res)
    order = {bid: i for i, (bid, _u) in enumerate(pairs)}
    results.sort(key=lambda r: order.get(r.id, 0))
    return results
