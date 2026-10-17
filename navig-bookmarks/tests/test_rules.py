"""Offline rule tagging."""

from __future__ import annotations

import pytest

from navig_bookmarks.rules import pattern_matches, tagger, tags_for


@pytest.mark.parametrize(
    ("pattern", "url", "hit"),
    [
        ("github.com", "https://github.com/a/b", True),
        ("github.com", "https://gist.github.com/x", True),
        ("example.com", "https://notexample.com/", False),  # label boundary (linkding matched this)
        ("https://youtube.com/watch", "https://www.youtube.com/watch?v=1", True),
        ("youtube.com/watch", "https://youtube.com/shorts/1", False),
        ("example.com/search?q", "https://example.com/search?q=cats", True),
        ("example.com/search?q", "https://example.com/search?x=1", False),
        ("example.com/search?lang=en", "https://example.com/search?lang=de", False),
        ("example.com/docs#api", "https://example.com/docs#api-auth", True),
        ("example.com/docs#api", "https://example.com/docs#guide", False),
        ("example.com", "not a url", False),
    ],
)
def test_pattern_matches(pattern: str, url: str, hit: bool) -> None:
    assert pattern_matches(pattern, url) is hit


def test_tags_for_merges_rules_in_order_without_repeats() -> None:
    rules = [(1, "github.com", ["dev", "code"]), (2, "github.com/python", ["python", "dev"]), (3, "x.com", ["social"])]
    assert tags_for("https://github.com/python/cpython", rules) == ["dev", "code", "python"]
    assert tagger([]) is None
    assert tagger(rules)("https://x.com/a") == ["social"]
