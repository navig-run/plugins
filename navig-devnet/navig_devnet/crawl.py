"""Read-only crawl + API discovery — the "map a service" half of DevNet.

Two engines, both standalone-clean (httpx + navig-sdk + stdlib, never navig):

  * :func:`crawl` — a polite, same-origin, robots-respecting link crawl. BFS from
    a start URL up to small page/depth caps, GET-only, every URL (and the
    robots.txt fetch) re-validated through navig-sdk's SSRF guard before any I/O.
    Returns the pages it saw with their status, title and outbound links — a
    sitemap you can hand to the agent or render in a tile.
  * :func:`discover_api` — probe the well-known OpenAPI/Swagger locations for a
    base URL, parse the spec (JSON natively; YAML best-effort if PyYAML is
    present) and return its endpoints (method · path · summary). Read-only.

Nothing here sends a body or mutates a target: it fetches with GET and reads.
The SSRF guard refuses private/internal targets unless the caller opts in
(``allow_private``), exactly like the repeater.
"""

from __future__ import annotations

import json
import urllib.parse
from collections import deque
from html.parser import HTMLParser
from typing import Any
from urllib import robotparser

from navig_sdk.ssrf import SsrfPolicy, check_url, policy_from_config

DEFAULT_TIMEOUT = 15.0
DEFAULT_USER_AGENT = "navig-devnet/crawl (+https://navig.run)"
#: Conservative defaults — a read-only crawl should map a service, not hammer it.
DEFAULT_MAX_PAGES = 25
MAX_PAGES_CAP = 200
DEFAULT_MAX_DEPTH = 2
MAX_DEPTH_CAP = 6
#: HTML we are willing to parse per page (links beyond this are not worth the memory).
_HTML_CAP = 2 * 1024 * 1024

#: Well-known spec locations tried by discover_api, in order of likelihood.
OPENAPI_CANDIDATES = (
    "/openapi.json",
    "/openapi.yaml",
    "/swagger.json",
    "/v3/api-docs",
    "/v2/swagger.json",
    "/api-docs",
    "/api/openapi.json",
    "/.well-known/openapi.json",
    "/swagger/v1/swagger.json",
)


class CrawlError(ValueError):
    """A crawl/discovery input was rejected, or the target could not be read."""


def _resolve_policy(allow_private: bool) -> SsrfPolicy:
    if allow_private:
        return SsrfPolicy(allow_private_network=True)
    return policy_from_config()


def _origin(url: str) -> tuple[str, str, int]:
    """scheme, host, effective port — the same-origin key."""
    p = urllib.parse.urlsplit(url)
    scheme = (p.scheme or "http").lower()
    host = (p.hostname or "").lower()
    port = p.port or (443 if scheme == "https" else 80)
    return scheme, host, port


def _same_origin(a: str, b: str) -> bool:
    return _origin(a) == _origin(b)


def _normalise(base: str, href: str) -> str | None:
    """Resolve *href* against *base*, drop the fragment, keep only http(s)."""
    try:
        joined = urllib.parse.urljoin(base, href.strip())
    except ValueError:
        return None
    joined, _frag = urllib.parse.urldefrag(joined)
    split = urllib.parse.urlsplit(joined)
    if split.scheme.lower() not in ("http", "https") or not split.hostname:
        return None
    return joined


class _LinkParser(HTMLParser):
    """Collect the document title and outbound ``href`` targets (a, link, area)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: str = ""
        self._in_title = False
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
            return
        if tag in ("a", "link", "area"):
            for name, value in attrs:
                if name == "href" and value:
                    self.hrefs.append(value)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title and len(self.title) < 300:
            self.title += data


def _parse_html(base: str, html: str) -> tuple[str, list[str]]:
    parser = _LinkParser()
    try:
        parser.feed(html[:_HTML_CAP])
    except Exception:  # noqa: BLE001 — a malformed document still yields what parsed
        pass
    links: list[str] = []
    seen: set[str] = set()
    for href in parser.hrefs:
        norm = _normalise(base, href)
        if norm and norm not in seen:
            seen.add(norm)
            links.append(norm)
    return parser.title.strip(), links


async def _fetch_robots(client, origin_url: str, policy: SsrfPolicy, timeout: float):
    """Fetch + parse robots.txt for an origin. On any failure, allow all (the
    conventional default for an unreachable/absent robots.txt)."""
    rp = robotparser.RobotFileParser()
    scheme, host, port = _origin(origin_url)
    netloc = host if port in (80, 443) else f"{host}:{port}"
    robots_url = f"{scheme}://{netloc}/robots.txt"
    try:
        check_url(robots_url, policy)
        resp = await client.request(
            "GET", robots_url, headers=None, content=None, timeout=_httpx_timeout(timeout)
        )
        if resp.status_code >= 400:
            rp.parse([])  # treated as allow-all
        else:
            rp.parse(resp.text.splitlines())
    except Exception:  # noqa: BLE001 — unreachable robots.txt ⇒ allow-all
        rp.parse([])
    return rp


def _httpx_timeout(timeout: float):
    import httpx  # noqa: PLC0415

    return httpx.Timeout(timeout)


async def crawl(
    start_url: str,
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_depth: int = DEFAULT_MAX_DEPTH,
    same_origin: bool = True,
    respect_robots: bool = True,
    allow_private: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
    user_agent: str = DEFAULT_USER_AGENT,
) -> dict[str, Any]:
    """Crawl from *start_url* and return ``{start, origin, pages, count, truncated,
    robots_respected}``.

    Each page is ``{url, status, title, content_type, depth, links}``. GET-only
    and read-only; every URL is SSRF-checked before fetch, off-origin links are
    not followed when *same_origin*, and ``robots.txt`` is honoured by default.
    Raises :class:`CrawlError` for a bad start URL; a per-page fetch error is
    recorded on that page (``status: 0``) and never aborts the crawl.
    """
    try:
        import httpx  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise ImportError("navig devnet crawl needs httpx (pip install httpx)") from exc

    start = _normalise(start_url, start_url) if "://" in start_url else _normalise("http://", start_url)
    if not start:
        raise CrawlError(f"invalid start URL: {start_url!r}")

    max_pages = max(1, min(int(max_pages), MAX_PAGES_CAP))
    max_depth = max(0, min(int(max_depth), MAX_DEPTH_CAP))
    policy = _resolve_policy(allow_private)
    check_url(start, policy)  # validate before any I/O

    headers = {"user-agent": user_agent, "accept": "text/html,application/xhtml+xml,*/*"}
    pages: list[dict[str, Any]] = []
    visited: set[str] = set()
    queue: deque[tuple[str, int]] = deque([(start, 0)])
    truncated = False
    rp = None

    async with httpx.AsyncClient(follow_redirects=True, headers=headers) as client:
        if respect_robots:
            rp = await _fetch_robots(client, start, policy, timeout)
        while queue:
            if len(pages) >= max_pages:
                truncated = True
                break
            url, depth = queue.popleft()
            if url in visited:
                continue
            visited.add(url)
            if rp is not None and not rp.can_fetch(user_agent, url):
                pages.append({"url": url, "status": 0, "title": "",
                              "content_type": "", "depth": depth, "links": [],
                              "error": "disallowed by robots.txt"})
                continue
            try:
                check_url(url, policy)  # re-validate every hop (redirects resolve client-side)
                resp = await client.request(
                    "GET", url, headers=None, content=None, timeout=_httpx_timeout(timeout)
                )
            except Exception as exc:  # noqa: BLE001 — one bad page never kills the crawl
                pages.append({"url": url, "status": 0, "title": "",
                              "content_type": "", "depth": depth, "links": [],
                              "error": f"{type(exc).__name__}: {exc}"})
                continue
            ctype = resp.headers.get("content-type", "")
            final_url = str(resp.url)
            title, links = ("", [])
            if "html" in ctype.lower():
                try:
                    title, links = _parse_html(final_url, resp.text)
                except Exception:  # noqa: BLE001 — undecodable body ⇒ no links
                    pass
            pages.append({
                "url": final_url, "status": resp.status_code, "title": title,
                "content_type": ctype, "depth": depth, "links": links,
            })
            if depth < max_depth:
                for link in links:
                    if link in visited:
                        continue
                    if same_origin and not _same_origin(start, link):
                        continue
                    queue.append((link, depth + 1))

    return {
        "start": start,
        "origin": "{}://{}".format(*_origin(start)[:2]),
        "pages": pages,
        "count": len(pages),
        "truncated": truncated,
        "robots_respected": bool(respect_robots),
    }


def _extract_endpoints(spec: dict[str, Any]) -> list[dict[str, str]]:
    """Pull ``{method, path, summary, operationId}`` rows out of an OpenAPI/Swagger
    ``paths`` object. Tolerant of both 2.0 and 3.x shapes."""
    endpoints: list[dict[str, str]] = []
    paths = spec.get("paths")
    if not isinstance(paths, dict):
        return endpoints
    verbs = ("get", "post", "put", "patch", "delete", "head", "options", "trace")
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        for verb in verbs:
            op = item.get(verb)
            if not isinstance(op, dict):
                continue
            endpoints.append({
                "method": verb.upper(),
                "path": str(path),
                "summary": str(op.get("summary") or op.get("description") or "").strip()[:200],
                "operation_id": str(op.get("operationId") or ""),
            })
    return endpoints


def _parse_spec(text: str, content_type: str) -> dict[str, Any] | None:
    """Parse a spec body as JSON, then YAML (best-effort). Returns the mapping or
    None if it is neither / not an OpenAPI document."""
    text = text.strip()
    if not text:
        return None
    # JSON first — the common /openapi.json case and the fastest.
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except ValueError:
        pass
    # YAML, only if PyYAML is available (not a hard dependency of this plugin).
    try:
        import yaml  # noqa: PLC0415

        obj = yaml.safe_load(text)
        return obj if isinstance(obj, dict) else None
    except Exception:  # noqa: BLE001 — no yaml, or not YAML
        return None


def _is_openapi(spec: dict[str, Any]) -> bool:
    return bool(spec.get("openapi") or spec.get("swagger")) and isinstance(spec.get("paths"), dict)


async def discover_api(
    base_url: str,
    *,
    allow_private: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
    user_agent: str = DEFAULT_USER_AGENT,
) -> dict[str, Any]:
    """Find and parse an OpenAPI/Swagger spec for *base_url*.

    If *base_url* points straight at a spec it is used directly; otherwise the
    well-known locations in :data:`OPENAPI_CANDIDATES` are probed under the URL's
    origin. Returns ``{spec_url, title, version, openapi, servers,
    endpoint_count, endpoints, tried}``. Read-only; every candidate is
    SSRF-checked. Raises :class:`CrawlError` if no spec is found.
    """
    try:
        import httpx  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise ImportError("navig devnet api needs httpx (pip install httpx)") from exc

    norm = _normalise(base_url, base_url) if "://" in base_url else _normalise("http://", base_url)
    if not norm:
        raise CrawlError(f"invalid URL: {base_url!r}")
    policy = _resolve_policy(allow_private)

    scheme, host, port = _origin(norm)
    netloc = host if port in (80, 443) else f"{host}:{port}"
    origin = f"{scheme}://{netloc}"
    path = urllib.parse.urlsplit(norm).path or "/"

    # Candidate order: the given URL first (it may BE the spec), then well-knowns.
    candidates: list[str] = []
    if path not in ("", "/"):
        candidates.append(norm)
    candidates.extend(origin + c for c in OPENAPI_CANDIDATES)

    headers = {"user-agent": user_agent, "accept": "application/json, application/yaml, */*"}
    tried: list[dict[str, Any]] = []

    async with httpx.AsyncClient(follow_redirects=True, headers=headers) as client:
        for cand in candidates:
            try:
                check_url(cand, policy)
                resp = await client.request(
                    "GET", cand, headers=None, content=None, timeout=_httpx_timeout(timeout)
                )
            except Exception as exc:  # noqa: BLE001
                tried.append({"url": cand, "status": 0, "error": f"{type(exc).__name__}: {exc}"})
                continue
            tried.append({"url": cand, "status": resp.status_code})
            if resp.status_code >= 400:
                continue
            spec = _parse_spec(resp.text, resp.headers.get("content-type", ""))
            if spec is None or not _is_openapi(spec):
                continue
            info = spec.get("info") if isinstance(spec.get("info"), dict) else {}
            servers = []
            if isinstance(spec.get("servers"), list):
                servers = [str(s.get("url", "")) for s in spec["servers"] if isinstance(s, dict)]
            elif spec.get("host"):  # swagger 2.0
                servers = [str(spec.get("host"))]
            endpoints = _extract_endpoints(spec)
            return {
                "spec_url": str(resp.url),
                "title": str(info.get("title") or "").strip(),
                "version": str(info.get("version") or "").strip(),
                "openapi": str(spec.get("openapi") or spec.get("swagger") or ""),
                "servers": servers,
                "endpoint_count": len(endpoints),
                "endpoints": endpoints,
                "tried": tried,
            }

    raise CrawlError(
        f"no OpenAPI/Swagger spec found for {base_url!r} "
        f"(tried {len(tried)} location(s))"
    )


def crawl_sync(start_url: str, **kwargs) -> dict[str, Any]:
    """Blocking wrapper around :func:`crawl` for the CLI."""
    import asyncio

    return asyncio.run(crawl(start_url, **kwargs))


def discover_api_sync(base_url: str, **kwargs) -> dict[str, Any]:
    """Blocking wrapper around :func:`discover_api` for the CLI."""
    import asyncio

    return asyncio.run(discover_api(base_url, **kwargs))
