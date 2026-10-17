"""Link checks: verdicts, HEAD->GET fallback, and every redirect hop SSRF-checked."""

from __future__ import annotations

import threading
import time

from navig_sdk.ssrf import SsrfPolicy

from navig_bookmarks import checker

STRICT = SsrfPolicy()  # never read the user's config in a test


def _script(monkeypatch, responses):
    """Stub the network: {(method, url): (status, location)}; record calls."""
    calls = []

    def fake_request(url, method, timeout):
        calls.append((method, url))
        return responses[(method, url)]

    monkeypatch.setattr(checker, "_request", fake_request)
    return calls


def _allow_public(monkeypatch):
    """check_url without DNS: block only the literal private hosts these tests use."""
    from navig_sdk import ssrf

    def fake_check(url, policy=None):
        host = url.split("/")[2].split(":")[0]
        if host in ("127.0.0.1", "169.254.169.254", "localhost"):
            raise ssrf.SsrfBlockedError(url, host)

    monkeypatch.setattr(ssrf, "check_url", fake_check)


def test_ok_and_dead(monkeypatch) -> None:
    _allow_public(monkeypatch)
    _script(monkeypatch, {("HEAD", "https://a.example/"): (200, None), ("HEAD", "https://b.example/"): (404, None)})
    assert checker.check_one("1", "https://a.example/", policy=STRICT).verdict == "ok"
    dead = checker.check_one("2", "https://b.example/", policy=STRICT)
    assert (dead.verdict, dead.status) == ("dead", 404)


def test_head_refused_falls_back_to_get(monkeypatch) -> None:
    _allow_public(monkeypatch)
    calls = _script(monkeypatch, {("HEAD", "https://a.example/"): (405, None), ("GET", "https://a.example/"): (200, None)})
    assert checker.check_one("1", "https://a.example/", policy=STRICT).verdict == "ok"
    assert calls == [("HEAD", "https://a.example/"), ("GET", "https://a.example/")]


def test_redirect_is_followed_and_recorded(monkeypatch) -> None:
    _allow_public(monkeypatch)
    _script(monkeypatch, {
        ("HEAD", "http://a.example/old"): (301, "https://a.example/new"),
        ("HEAD", "https://a.example/new"): (200, None),
    })
    res = checker.check_one("1", "http://a.example/old", policy=STRICT)
    assert (res.verdict, res.status, res.final_url) == ("redirect", 200, "https://a.example/new")


def test_a_redirect_into_a_private_address_is_blocked_before_it_is_requested(monkeypatch) -> None:
    _allow_public(monkeypatch)
    calls = _script(monkeypatch, {("HEAD", "https://a.example/"): (302, "http://169.254.169.254/latest/meta-data")})
    res = checker.check_one("1", "https://a.example/", policy=STRICT)
    assert res.verdict == "blocked"
    assert calls == [("HEAD", "https://a.example/")]  # the metadata address was never contacted


def test_a_private_url_is_blocked_by_the_real_guard() -> None:
    """No stubs: navig-sdk's guard refuses loopback without any network I/O."""
    res = checker.check_one("1", "http://127.0.0.1:9/", policy=STRICT)
    assert res.verdict == "blocked"


def test_non_http_is_skipped() -> None:
    assert checker.check_one("1", "javascript:void(0)", policy=STRICT).verdict == "skipped"
    assert checker.check_one("2", "file:///etc/passwd", policy=STRICT).verdict == "skipped"


def test_restricted_and_server_errors(monkeypatch) -> None:
    _allow_public(monkeypatch)
    _script(monkeypatch, {
        ("HEAD", "https://a.example/"): (403, None), ("GET", "https://a.example/"): (403, None),
        ("HEAD", "https://b.example/"): (503, None),
    })
    assert checker.check_one("1", "https://a.example/", policy=STRICT).verdict == "restricted"
    assert checker.check_one("2", "https://b.example/", policy=STRICT).verdict == "error"


def test_redirect_loops_stop(monkeypatch) -> None:
    _allow_public(monkeypatch)
    _script(monkeypatch, {("HEAD", "https://a.example/"): (302, "https://a.example/")})
    res = checker.check_one("1", "https://a.example/", policy=STRICT)
    assert res.verdict == "error" and "redirects" in res.error


def test_check_many_limits_per_host_and_keeps_order(monkeypatch) -> None:
    monkeypatch.setattr(checker, "_policy", lambda: STRICT)
    active: dict[str, int] = {}
    peak: dict[str, int] = {}
    lock = threading.Lock()

    def fake_checker(bid, url, *, timeout, policy):
        host = url.split("/")[2]
        with lock:
            active[host] = active.get(host, 0) + 1
            peak[host] = max(peak.get(host, 0), active[host])
        time.sleep(0.02)
        with lock:
            active[host] -= 1
        return checker.CheckResult(bid, url, "dead" if bid == "3" else "ok")

    items = [(str(i), f"https://{'same' if i % 2 else 'other'}.example/{i}") for i in range(10)]
    looked_up = []
    monkeypatch.setattr(checker, "wayback_snapshot", lambda url, timeout: looked_up.append(url) or "https://web.archive.org/x")
    results = checker.check_many(items, concurrency=8, per_host=2, checker=fake_checker)
    assert [r.id for r in results] == [str(i) for i in range(10)]
    assert max(peak.values()) <= 2
    assert looked_up == ["https://same.example/3"]
    assert results[3].wayback == "https://web.archive.org/x"
