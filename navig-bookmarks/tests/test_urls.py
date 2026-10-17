"""URL normalisation: the duplicate key must merge the same page and nothing else."""

from __future__ import annotations

import pytest

from navig_bookmarks.urls import dedupe_key, ensure_scheme, looks_like_url, normalize_url, site_of


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTPS://Example.COM/Path/", "https://example.com/Path"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("http://example.com:80/a", "http://example.com/a"),
        ("https://example.com:8443/a", "https://example.com:8443/a"),
        ("https://example.com/?b=2&a=1", "https://example.com?a=1&b=2"),
        ("https://example.com/a?utm_source=x&utm_medium=y&id=7", "https://example.com/a?id=7"),
        ("https://example.com/a?fbclid=abc&gclid=def", "https://example.com/a"),
        ("https://example.com/a#Section", "https://example.com/a#Section"),
        ("https://user:pw@Example.com/a", "https://user:pw@example.com/a"),
        ("  https://example.com/a  ", "https://example.com/a"),
        ("javascript:alert(1)", "javascript:alert(1)"),
        ("mailto:Someone@Example.com", "mailto:Someone@Example.com"),
        ("", ""),
    ],
)
def test_normalize_url(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


def test_normalize_keeps_meaningful_short_params() -> None:
    # `ref` and `source` mean something on many sites; only known trackers are dropped.
    assert normalize_url("https://example.com/?ref=main&source=docs") == "https://example.com?ref=main&source=docs"


def test_tracking_strip_can_be_disabled() -> None:
    assert "utm_source" in normalize_url("https://e.com/?utm_source=x", strip_tracking=False)


def test_normalize_is_idempotent() -> None:
    url = "HTTP://www.Example.com:80/a/b/?z=1&utm_campaign=q&a=2#frag"
    once = normalize_url(url)
    assert normalize_url(once) == once


@pytest.mark.parametrize(
    "variant",
    [
        "http://www.example.com/page/?utm_source=newsletter",
        "https://example.com/page",
        "https://EXAMPLE.com/page/",
        "https://www.example.com:443/page?fbclid=1",
    ],
)
def test_dedupe_key_merges_the_same_page(variant: str) -> None:
    assert dedupe_key(variant) == dedupe_key("https://example.com/page")


@pytest.mark.parametrize(
    "different",
    ["https://example.com/page2", "https://example.com/page?id=2", "https://docs.example.com/page",
     "https://example.com/page#other"],
)
def test_dedupe_key_keeps_different_pages_apart(different: str) -> None:
    assert dedupe_key(different) != dedupe_key("https://example.com/page")


def test_looks_like_url_and_scheme() -> None:
    assert looks_like_url("https://example.com")
    assert looks_like_url("example.com/path?q=1")
    assert not looks_like_url("list")
    assert not looks_like_url("two words.com")
    assert ensure_scheme("example.com") == "https://example.com"
    assert ensure_scheme("http://example.com") == "http://example.com"


@pytest.mark.parametrize(
    ("url", "site"),
    [
        ("https://docs.python.org/3/", "python.org"),
        ("https://www.bbc.co.uk/news", "bbc.co.uk"),
        ("https://github.com/a/b", "github.com"),
        ("http://192.168.1.10:8080/", "192.168.1.10"),
        ("not a url", ""),
    ],
)
def test_site_of(url: str, site: str) -> None:
    assert site_of(url) == site
