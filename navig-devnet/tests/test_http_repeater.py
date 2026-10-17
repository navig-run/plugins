"""HTTP repeater engine — hermetic. Mocks httpx + the SSRF check_url so no real
DNS or network happens; proves method/body plumbing, response shaping, redirect
RE-validation, and that a blocked target is refused before any I/O."""

from __future__ import annotations

import asyncio

import pytest

from navig_devnet import http_repeater as hr


class _FakeResp:
    def __init__(self, status, headers=None, content=b"hi", url="http://x/"):
        self.status_code = status
        self.reason_phrase = "OK" if status < 400 else "ERR"
        self.headers = headers or {"content-type": "text/plain"}
        self.content = content
        self.text = content.decode() if isinstance(content, bytes) else content
        self.url = url

    @property
    def is_redirect(self):
        return self.status_code in (301, 302, 303, 307, 308)


class _FakeClient:
    """Async-context httpx stand-in. `script` is a list of responses to return in order."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def request(self, method, url, headers=None, content=None, timeout=None):
        self.calls.append((method, url, content))
        return self._script.pop(0)


def _install(monkeypatch, script):
    import httpx

    monkeypatch.setattr(hr, "check_url", lambda url, policy=None: None)  # no real DNS
    client = _FakeClient(script)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: client)
    return client


def test_post_sends_method_and_body(monkeypatch):
    client = _install(monkeypatch, [_FakeResp(200, content=b"created")])
    out = asyncio.run(hr.send("post", "http://api.example/x", body="payload"))
    assert out["response"]["status"] == 200
    assert out["response"]["body"] == "created"
    assert out["request"]["method"] == "POST"
    # method + body actually reached the client
    assert client.calls[0][0] == "POST"
    assert client.calls[0][2] == b"payload"


def test_redirect_is_revalidated_and_followed(monkeypatch):
    seen = []
    script = [
        _FakeResp(302, headers={"location": "http://next.example/here"}),
        _FakeResp(200, content=b"final"),
    ]
    _install(monkeypatch, script)
    monkeypatch.setattr(hr, "check_url", lambda url, policy=None: seen.append(url))
    out = asyncio.run(hr.send("GET", "http://start.example/"))
    assert out["response"]["body"] == "final"
    assert out["redirects"][0]["to"] == "http://next.example/here"
    # the redirect target was re-validated (initial + hop)
    assert "http://next.example/here" in seen


def test_no_redirect_returns_the_302(monkeypatch):
    _install(monkeypatch, [_FakeResp(302, headers={"location": "http://next/"})])
    out = asyncio.run(hr.send("GET", "http://start/", follow_redirects=False))
    assert out["response"]["status"] == 302
    assert out["redirects"] == []


def test_blocked_target_raises_before_io(monkeypatch):
    from navig_sdk.ssrf import SsrfBlockedError

    def boom(url, policy=None):
        raise SsrfBlockedError("blocked", url)

    monkeypatch.setattr(hr, "check_url", boom)
    with pytest.raises(SsrfBlockedError):
        asyncio.run(hr.send("GET", "http://169.254.169.254/"))


def test_unknown_method_rejected():
    with pytest.raises(ValueError, match="unsupported method"):
        asyncio.run(hr.send("TRACE", "http://x/"))
