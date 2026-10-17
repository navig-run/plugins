"""DevNet deck API (/api/deck/devnet/*) — route mounting + handler JSON, engine
mocked (no network). Needs navig for the gate; skipped cleanly without it."""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("aiohttp")
pytest.importorskip("navig.modules.gate", reason="deck routes gate needs navig")

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import make_mocked_request  # noqa: E402

from navig_devnet.deck_routes import devnet as dr  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _body(resp) -> dict:
    return json.loads(resp.body)


def test_register_mounts_all_routes():
    app = web.Application()
    dr.register(app)
    paths = {r.resource.canonical for r in app.router.routes()}
    assert paths == {
        "/api/deck/devnet/http",
        "/api/deck/devnet/history",
        "/api/deck/devnet/crawl",
        "/api/deck/devnet/api",
    }


def test_http_sends_and_records(monkeypatch):
    sent = {}

    async def fake_send(method, url, **kw):
        sent["method"], sent["url"], sent["kw"] = method, url, kw
        return {"request": {"method": method, "url": url},
                "response": {"status": 200, "body": "ok", "body_len": 2},
                "redirects": [], "elapsed_ms": 1.0}

    monkeypatch.setattr("navig_devnet.http_repeater.send", fake_send)
    monkeypatch.setattr("navig_devnet.history.record", lambda r: 7)

    req = make_mocked_request("POST", "/api/deck/devnet/http")
    req.json = lambda: _ajson({"method": "post", "url": "http://x/", "body": "hi"})()
    resp = _run(dr.handle_http(req))
    data = _body(resp)
    assert resp.status == 200 and data["ok"] is True
    assert data["data"]["history_id"] == 7
    assert sent["method"] == "post" and sent["url"] == "http://x/"


def test_http_missing_url_is_400(monkeypatch):
    req = make_mocked_request("POST", "/api/deck/devnet/http")
    req.json = lambda: _ajson({"method": "GET"})()
    resp = _run(dr.handle_http(req))
    assert resp.status == 400 and _body(resp)["ok"] is False


def test_http_ssrf_block_is_400(monkeypatch):
    from navig_sdk.ssrf import SsrfBlockedError

    async def boom(method, url, **kw):
        raise SsrfBlockedError("blocked", url)

    monkeypatch.setattr("navig_devnet.http_repeater.send", boom)
    req = make_mocked_request("POST", "/api/deck/devnet/http")
    req.json = lambda: _ajson({"url": "http://169.254.169.254/"})()
    resp = _run(dr.handle_http(req))
    assert resp.status == 400 and "blocked" in _body(resp)["error"]


def test_history_returns_list(monkeypatch):
    monkeypatch.setattr("navig_devnet.history.load", lambda limit=50: [{"id": 1, "url": "http://a/"}])
    req = make_mocked_request("GET", "/api/deck/devnet/history?limit=10")
    resp = _run(dr.handle_history(req))
    data = _body(resp)
    assert data["ok"] is True and data["data"][0]["id"] == 1


def test_crawl_returns_map(monkeypatch):
    seen = {}

    async def fake_crawl(url, **kw):
        seen["url"], seen["kw"] = url, kw
        return {"start": url, "origin": "http://x", "count": 1, "truncated": False,
                "robots_respected": True,
                "pages": [{"url": url, "status": 200, "title": "H", "content_type": "text/html",
                           "depth": 0, "links": []}]}

    monkeypatch.setattr("navig_devnet.crawl.crawl", fake_crawl)
    req = make_mocked_request("POST", "/api/deck/devnet/crawl")
    req.json = lambda: _ajson({"url": "http://x/", "max_pages": 5, "respect_robots": True})()
    resp = _run(dr.handle_crawl(req))
    data = _body(resp)
    assert resp.status == 200 and data["ok"] is True
    assert data["data"]["count"] == 1
    assert seen["url"] == "http://x/" and seen["kw"]["max_pages"] == 5


def test_crawl_missing_url_is_400():
    req = make_mocked_request("POST", "/api/deck/devnet/crawl")
    req.json = lambda: _ajson({"max_pages": 5})()
    resp = _run(dr.handle_crawl(req))
    assert resp.status == 400 and _body(resp)["ok"] is False


def test_crawl_ssrf_block_is_400(monkeypatch):
    from navig_sdk.ssrf import SsrfBlockedError

    async def boom(url, **kw):
        raise SsrfBlockedError("blocked", url)

    monkeypatch.setattr("navig_devnet.crawl.crawl", boom)
    req = make_mocked_request("POST", "/api/deck/devnet/crawl")
    req.json = lambda: _ajson({"url": "http://10.0.0.1/"})()
    resp = _run(dr.handle_crawl(req))
    assert resp.status == 400 and "blocked" in _body(resp)["error"]


def test_api_returns_endpoints(monkeypatch):
    async def fake_discover(url, **kw):
        return {"spec_url": url + "openapi.json", "title": "Pet", "version": "1.0",
                "openapi": "3.0.0", "servers": [], "endpoint_count": 1,
                "endpoints": [{"method": "GET", "path": "/pets", "summary": "list", "operation_id": "l"}]}

    monkeypatch.setattr("navig_devnet.crawl.discover_api", fake_discover)
    req = make_mocked_request("POST", "/api/deck/devnet/api")
    req.json = lambda: _ajson({"url": "http://api.example/"})()
    resp = _run(dr.handle_api(req))
    data = _body(resp)
    assert resp.status == 200 and data["data"]["endpoint_count"] == 1
    assert data["data"]["endpoints"][0]["path"] == "/pets"


def test_api_not_found_is_404(monkeypatch):
    from navig_devnet.crawl import CrawlError

    async def boom(url, **kw):
        raise CrawlError("no OpenAPI/Swagger spec found")

    monkeypatch.setattr("navig_devnet.crawl.discover_api", boom)
    req = make_mocked_request("POST", "/api/deck/devnet/api")
    req.json = lambda: _ajson({"url": "http://api.example/"})()
    resp = _run(dr.handle_api(req))
    assert resp.status == 404 and "no OpenAPI" in _body(resp)["error"]


def _ajson(payload):
    async def _j():
        return payload
    return _j
