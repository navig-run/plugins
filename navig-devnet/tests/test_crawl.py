"""crawl + API-discovery engine — hermetic. Mocks httpx + the SSRF check_url so
no real DNS or network happens; proves BFS/same-origin/depth limits, robots.txt
honouring, SSRF refusal of the start URL, and OpenAPI candidate probing + parse."""

from __future__ import annotations

import json

import pytest

from navig_devnet import crawl as cr


class _FakeResp:
    def __init__(self, status=200, content_type="text/html", body="", url="http://x/"):
        self.status_code = status
        self.headers = {"content-type": content_type}
        self._body = body
        self.url = url

    @property
    def text(self):
        return self._body


class _FakeClient:
    """Async-context httpx stand-in. ``routes`` maps a URL to a _FakeResp (or a
    callable). An unknown URL returns 404."""

    def __init__(self, routes):
        self.routes = routes
        self.requested = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def request(self, method, url, headers=None, content=None, timeout=None):
        self.requested.append(url)
        r = self.routes.get(url)
        if r is None:
            return _FakeResp(status=404, body="", url=url)
        if callable(r):
            return r(url)
        return r


def _install(monkeypatch, routes, *, record_checks=None):
    import httpx

    if record_checks is None:
        monkeypatch.setattr(cr, "check_url", lambda url, policy=None: None)
    else:
        monkeypatch.setattr(cr, "check_url", lambda url, policy=None: record_checks.append(url))
    client = _FakeClient(routes)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: client)
    # make httpx.Timeout harmless
    monkeypatch.setattr(httpx, "Timeout", lambda t: t)
    return client


def _html(title, hrefs):
    links = "".join(f'<a href="{h}">x</a>' for h in hrefs)
    return f"<html><head><title>{title}</title></head><body>{links}</body></html>"


# ── crawl ──────────────────────────────────────────────────────────────────
def test_crawl_follows_same_origin_links_bfs(monkeypatch):
    routes = {
        "http://site.example/robots.txt": _FakeResp(status=404),
        "http://site.example/": _FakeResp(body=_html("Home", ["/a", "/b", "http://other.example/x"]),
                                          url="http://site.example/"),
        "http://site.example/a": _FakeResp(body=_html("A", []), url="http://site.example/a"),
        "http://site.example/b": _FakeResp(body=_html("B", []), url="http://site.example/b"),
    }
    _install(monkeypatch, routes)
    out = cr.crawl_sync("http://site.example/", max_pages=25, max_depth=2)
    urls = {p["url"] for p in out["pages"]}
    assert urls == {"http://site.example/", "http://site.example/a", "http://site.example/b"}
    # off-origin link was discovered but NOT crawled (same_origin default)
    assert "http://other.example/x" not in urls
    home = next(p for p in out["pages"] if p["url"] == "http://site.example/")
    assert home["title"] == "Home"
    assert out["count"] == 3


def test_crawl_respects_max_pages(monkeypatch):
    routes = {"http://site.example/robots.txt": _FakeResp(status=404),
              "http://site.example/": _FakeResp(body=_html("Home", ["/a", "/b", "/c"]),
                                                 url="http://site.example/")}
    for p in ("a", "b", "c"):
        routes[f"http://site.example/{p}"] = _FakeResp(body=_html(p, []), url=f"http://site.example/{p}")
    _install(monkeypatch, routes)
    out = cr.crawl_sync("http://site.example/", max_pages=2, max_depth=3)
    assert out["count"] == 2
    assert out["truncated"] is True


def test_crawl_depth_limit_stops_recursion(monkeypatch):
    routes = {
        "http://site.example/robots.txt": _FakeResp(status=404),
        "http://site.example/": _FakeResp(body=_html("Home", ["/a"]), url="http://site.example/"),
        "http://site.example/a": _FakeResp(body=_html("A", ["/b"]), url="http://site.example/a"),
        "http://site.example/b": _FakeResp(body=_html("B", []), url="http://site.example/b"),
    }
    _install(monkeypatch, routes)
    out = cr.crawl_sync("http://site.example/", max_depth=1)
    urls = {p["url"] for p in out["pages"]}
    # depth 0 = home, depth 1 = /a; /b is depth 2 and must not be fetched
    assert urls == {"http://site.example/", "http://site.example/a"}


def test_crawl_honours_robots_disallow(monkeypatch):
    robots = "User-agent: *\nDisallow: /private\n"
    routes = {
        "http://site.example/robots.txt": _FakeResp(content_type="text/plain", body=robots),
        "http://site.example/": _FakeResp(body=_html("Home", ["/private", "/ok"]),
                                           url="http://site.example/"),
        "http://site.example/ok": _FakeResp(body=_html("OK", []), url="http://site.example/ok"),
        "http://site.example/private": _FakeResp(body=_html("secret", []), url="http://site.example/private"),
    }
    client = _install(monkeypatch, routes)
    out = cr.crawl_sync("http://site.example/")
    priv = next(p for p in out["pages"] if p["url"].endswith("/private"))
    assert priv["status"] == 0 and "robots" in priv["error"]
    # the disallowed URL was never actually fetched
    assert "http://site.example/private" not in client.requested
    assert out["robots_respected"] is True


def test_crawl_ignore_robots_fetches_everything(monkeypatch):
    robots = "User-agent: *\nDisallow: /private\n"
    routes = {
        "http://site.example/robots.txt": _FakeResp(content_type="text/plain", body=robots),
        "http://site.example/": _FakeResp(body=_html("Home", ["/private"]), url="http://site.example/"),
        "http://site.example/private": _FakeResp(body=_html("secret", []), url="http://site.example/private"),
    }
    client = _install(monkeypatch, routes)
    out = cr.crawl_sync("http://site.example/", respect_robots=False)
    assert "http://site.example/private" in client.requested
    assert out["robots_respected"] is False


def test_crawl_blocked_start_raises_before_io(monkeypatch):
    from navig_sdk.ssrf import SsrfBlockedError

    def boom(url, policy=None):
        raise SsrfBlockedError("blocked", url)

    monkeypatch.setattr(cr, "check_url", boom)
    with pytest.raises(SsrfBlockedError):
        cr.crawl_sync("http://169.254.169.254/")


def test_crawl_bad_start_url_raises():
    with pytest.raises(cr.CrawlError):
        cr.crawl_sync("not a url at all /// :::")


def test_crawl_per_page_error_does_not_abort(monkeypatch):
    def transport_error(url):
        raise RuntimeError("connection reset")

    routes = {
        "http://site.example/robots.txt": _FakeResp(status=404),
        "http://site.example/": _FakeResp(body=_html("Home", ["/a", "/b"]), url="http://site.example/"),
        "http://site.example/a": transport_error,
        "http://site.example/b": _FakeResp(body=_html("B", []), url="http://site.example/b"),
    }
    _install(monkeypatch, routes)
    out = cr.crawl_sync("http://site.example/")
    bad = next(p for p in out["pages"] if p["url"].endswith("/a"))
    assert bad["status"] == 0 and "connection reset" in bad["error"]
    # the crawl continued to /b despite /a failing
    assert any(p["url"].endswith("/b") and p["status"] == 200 for p in out["pages"])


# ── discover_api ──────────────────────────────────────────────────────────
_SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Pet API", "version": "1.2.3"},
    "servers": [{"url": "https://api.pets.example/v1"}],
    "paths": {
        "/pets": {
            "get": {"summary": "List pets", "operationId": "listPets"},
            "post": {"summary": "Create a pet"},
        },
        "/pets/{id}": {"get": {"description": "Get one pet"}},
    },
}


def test_discover_api_finds_spec_at_candidate(monkeypatch):
    routes = {
        "http://api.example/openapi.json": _FakeResp(
            content_type="application/json", body=json.dumps(_SPEC),
            url="http://api.example/openapi.json"),
    }
    _install(monkeypatch, routes)
    out = cr.discover_api_sync("http://api.example")
    assert out["title"] == "Pet API"
    assert out["version"] == "1.2.3"
    assert out["endpoint_count"] == 3
    methods = {(e["method"], e["path"]) for e in out["endpoints"]}
    assert ("GET", "/pets") in methods and ("POST", "/pets") in methods
    assert ("GET", "/pets/{id}") in methods
    # summary falls back to description
    one = next(e for e in out["endpoints"] if e["path"] == "/pets/{id}")
    assert one["summary"] == "Get one pet"


def test_discover_api_direct_spec_url(monkeypatch):
    routes = {
        "http://api.example/custom/spec.json": _FakeResp(
            content_type="application/json", body=json.dumps(_SPEC),
            url="http://api.example/custom/spec.json"),
    }
    client = _install(monkeypatch, routes)
    out = cr.discover_api_sync("http://api.example/custom/spec.json")
    assert out["endpoint_count"] == 3
    # the given URL was tried FIRST (before the well-known candidates)
    assert client.requested[0] == "http://api.example/custom/spec.json"


def test_discover_api_skips_non_openapi_json(monkeypatch):
    # /openapi.json returns valid JSON that is NOT an OpenAPI doc → keep probing
    routes = {
        "http://api.example/openapi.json": _FakeResp(
            content_type="application/json", body=json.dumps({"hello": "world"}),
            url="http://api.example/openapi.json"),
        "http://api.example/swagger.json": _FakeResp(
            content_type="application/json", body=json.dumps(_SPEC),
            url="http://api.example/swagger.json"),
    }
    _install(monkeypatch, routes)
    out = cr.discover_api_sync("http://api.example")
    assert out["spec_url"] == "http://api.example/swagger.json"
    assert out["endpoint_count"] == 3


def test_discover_api_not_found_raises(monkeypatch):
    _install(monkeypatch, {})  # every candidate 404s
    with pytest.raises(cr.CrawlError, match="no OpenAPI"):
        cr.discover_api_sync("http://api.example")


def test_discover_api_swagger_2_host(monkeypatch):
    spec2 = {"swagger": "2.0", "host": "api.old.example",
             "info": {"title": "Legacy"}, "paths": {"/x": {"get": {"summary": "x"}}}}
    routes = {
        "http://api.example/openapi.json": _FakeResp(
            content_type="application/json", body=json.dumps(spec2),
            url="http://api.example/openapi.json"),
    }
    _install(monkeypatch, routes)
    out = cr.discover_api_sync("http://api.example")
    assert out["openapi"] == "2.0"
    assert out["servers"] == ["api.old.example"]


def test_discover_api_blocked_target_raises(monkeypatch):
    from navig_sdk.ssrf import SsrfBlockedError

    def boom(url, policy=None):
        raise SsrfBlockedError("blocked", url)

    monkeypatch.setattr(cr, "check_url", boom)
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _FakeClient({}))
    monkeypatch.setattr(httpx, "Timeout", lambda t: t)
    # every candidate is blocked → recorded as tried-with-error, then not-found
    with pytest.raises(cr.CrawlError, match="no OpenAPI"):
        cr.discover_api_sync("http://10.0.0.1")
