"""Unit tests for the yt-dlp anti-detection helpers (Stage 1)."""
from __future__ import annotations

import navig_download.anti_detect as ad


def test_impersonate_coherence_drops_conflicting_ua(monkeypatch):
    """When impersonate is active, a pre-set (possibly non-Chrome) UA is dropped so
    curl_cffi's coherent Chrome UA + sec-ch-ua win."""
    monkeypatch.setattr(ad, "impersonate_target", lambda: "chrome")
    opts = {"http_headers": {"User-Agent": "Firefox/121.0"}}
    ad.apply_anti_detect(opts)
    assert opts["impersonate"] == "chrome"
    assert "User-Agent" not in opts["http_headers"]
    assert opts["http_headers"]["Accept-Language"] == "en-US,en;q=0.9"


def test_caller_ua_survives_impersonate(monkeypatch):
    """An explicit caller UA is assumed coherent and kept even under impersonate."""
    monkeypatch.setattr(ad, "impersonate_target", lambda: "chrome")
    opts: dict = {}
    ad.apply_anti_detect(opts, ua="MyUA/1.0")
    assert opts["http_headers"]["User-Agent"] == "MyUA/1.0"
    assert opts["impersonate"] == "chrome"


def test_no_impersonate_keeps_rotating_ua(monkeypatch):
    """With no impersonate backend, a plausible UA is set as the fallback identity."""
    monkeypatch.setattr(ad, "impersonate_target", lambda: None)
    opts: dict = {}
    ad.apply_anti_detect(opts)
    assert "impersonate" not in opts
    assert opts["http_headers"]["User-Agent"] in ad.USER_AGENTS


def test_no_impersonate_preserves_existing_ua(monkeypatch):
    monkeypatch.setattr(ad, "impersonate_target", lambda: None)
    opts = {"http_headers": {"User-Agent": "Existing/9"}}
    ad.apply_anti_detect(opts)
    assert opts["http_headers"]["User-Agent"] == "Existing/9"


def test_proxy_and_cookies_wired(monkeypatch):
    monkeypatch.setattr(ad, "impersonate_target", lambda: None)
    opts: dict = {}
    ad.apply_anti_detect(opts, proxy="socks5://h:1", cookiefile="c.txt",
                         cookiesfrombrowser=("chrome",))
    assert opts["proxy"] == "socks5://h:1"
    assert opts["cookiefile"] == "c.txt"
    assert opts["cookiesfrombrowser"] == ("chrome",)


def test_no_optional_keys_when_unset(monkeypatch):
    monkeypatch.setattr(ad, "impersonate_target", lambda: None)
    opts: dict = {}
    ad.apply_anti_detect(opts)
    for k in ("proxy", "cookiefile", "cookiesfrombrowser"):
        assert k not in opts


def test_resolve_fetch_defaults_prefers_args(monkeypatch):
    monkeypatch.setenv("NAVIG_TIKTOK_PROXY", "http://env:1")
    p, cf, cfb = ad.resolve_fetch_defaults(proxy="http://arg:2")
    assert p == "http://arg:2"  # explicit arg beats env


def test_resolve_fetch_defaults_reads_env(monkeypatch):
    monkeypatch.setenv("NAVIG_TIKTOK_PROXY", "http://env:1")
    monkeypatch.setenv("NAVIG_TIKTOK_COOKIES", "/tmp/c.txt")
    monkeypatch.setenv("NAVIG_TIKTOK_COOKIES_FROM_BROWSER", "edge")
    p, cf, cfb = ad.resolve_fetch_defaults()
    assert p == "http://env:1"
    assert cf == "/tmp/c.txt"
    assert cfb == ("edge",)  # wrapped in the tuple yt-dlp expects


def test_resolve_fetch_defaults_empty(monkeypatch):
    for k in ("NAVIG_TIKTOK_PROXY", "NAVIG_TIKTOK_COOKIES", "NAVIG_TIKTOK_COOKIES_FROM_BROWSER"):
        monkeypatch.delenv(k, raising=False)
    assert ad.resolve_fetch_defaults() == (None, None, None)


def test_looks_blocked_true_markers():
    for msg in ("HTTP Error 403: Forbidden", "HTTP Error 429", "please solve the captcha",
                "rate limit exceeded", "Unable to extract webpage", "Sign in to confirm"):
        assert ad.looks_blocked(msg), msg


def test_looks_blocked_false_on_benign():
    for msg in ("no comments", "video has no description", "download complete", ""):
        assert not ad.looks_blocked(msg), msg
