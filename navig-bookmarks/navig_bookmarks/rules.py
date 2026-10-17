"""Offline tagging rules: ``pattern -> tags``, no AI, no network.

Adapted from linkding's ``services/auto_tagging.py`` (MIT, see THIRD_PARTY_NOTICES.md).
A pattern is a URL without the scheme; every part present must match:

    github.com                 any page on github.com or a subdomain   -> dev
    youtube.com/watch          paths starting with /watch               -> video
    example.com/search?q       a query that has a ``q`` parameter
    example.com/docs#api       a fragment starting with ``api``

One fix over the original: the domain must match on a label boundary, so ``example.com``
matches ``docs.example.com`` but NOT ``notexample.com`` (linkding compared with a bare
``endswith``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import parse_qs, urlparse


def _idna(host: str) -> str:
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def _domain_matches(expected: str, actual: str) -> bool:
    expected, actual = _idna(expected.lower()), _idna(actual.lower())
    return actual == expected or actual.endswith("." + expected)


def _qs_matches(expected_qs: str, actual_qs: str) -> bool:
    expected = parse_qs(expected_qs, keep_blank_values=True)
    actual = parse_qs(actual_qs, keep_blank_values=True)
    for key, values in expected.items():
        if key not in actual:
            return False
        for value in values:
            if value != "" and value not in actual[key]:
                return False
    return True


def pattern_matches(pattern: str, url: str) -> bool:
    """Does *url* satisfy *pattern* (see the module docstring)?"""
    parsed_url = urlparse(url.strip())
    if not parsed_url.hostname:
        return False
    pat = urlparse("//" + re.sub(r"^[a-z]+://", "", pattern.strip(), flags=re.I))
    if not pat.hostname or not _domain_matches(pat.hostname, parsed_url.hostname):
        return False
    if pat.path and pat.path != "/" and not parsed_url.path.lower().startswith(pat.path.lower()):
        return False
    if pat.query and not _qs_matches(pat.query, parsed_url.query):
        return False
    if pat.fragment and not parsed_url.fragment.startswith(pat.fragment):
        return False
    return True


def tags_for(url: str, rules: Iterable[tuple[int, str, list[str]]]) -> list[str]:
    """Every tag the *rules* give *url*, in rule order, without repeats."""
    out: list[str] = []
    for _rid, pattern, tags in rules:
        if pattern_matches(pattern, url):
            for t in tags:
                if t not in out:
                    out.append(t)
    return out


def tagger(rules: list[tuple[int, str, list[str]]]):
    """A ``url -> tags`` callable over a fixed rule list (``None`` when there are no rules)."""
    if not rules:
        return None
    return lambda url: tags_for(url, rules)
