"""Tests for the ported song.link / Odesli music-link resolver.

Migrated from the retired ``telegram-bot-navig`` pack — this is the regression
test proving the capability survived the move into navig-download.
"""

from __future__ import annotations

import pytest

from navig_download import music_links


def test_find_music_url_detects_known_services():
    text = "check this out https://open.spotify.com/track/abc123 great song"
    assert music_links.find_music_url(text) == "https://open.spotify.com/track/abc123"


def test_find_music_url_returns_none_for_non_music():
    assert music_links.find_music_url("just some https://example.com/page text") is None
    assert music_links.find_music_url("") is None


def test_parse_odesli_normalizes_response():
    raw = {
        "entityUniqueId": "SPOTIFY_SONG::abc",
        "pageUrl": "https://song.link/s/abc",
        "entitiesByUniqueId": {
            "SPOTIFY_SONG::abc": {"title": "Song", "artistName": "Artist"},
        },
        "linksByPlatform": {
            "spotify": {"url": "https://open.spotify.com/track/abc"},
            "appleMusic": {"url": "https://music.apple.com/track/abc"},
            "someNewService": {"url": "https://new.example/abc"},
        },
    }
    result = music_links.parse_odesli(raw)

    assert result["title"] == "Song"
    assert result["artist"] == "Artist"
    assert result["page_url"] == "https://song.link/s/abc"

    by_platform = {link["platform"]: link for link in result["links"]}
    assert by_platform["spotify"]["label"] == "Spotify"
    assert by_platform["appleMusic"]["label"] == "Apple Music"
    # unknown platform keys fall back to Title-case, never crash
    assert by_platform["someNewService"]["label"] == "SomeNewService"


def test_parse_odesli_raises_when_no_links():
    with pytest.raises(music_links.MusicResolveError):
        music_links.parse_odesli({"linksByPlatform": {}})


def test_resolve_links_uses_requests(monkeypatch):
    captured: dict = {}

    class _FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "entityUniqueId": "X",
                "entitiesByUniqueId": {"X": {"title": "T", "artistName": "A"}},
                "linksByPlatform": {"spotify": {"url": "https://open.spotify.com/track/x"}},
            }

    def _fake_get(url, params=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        return _FakeResp()

    import requests

    monkeypatch.setattr(requests, "get", _fake_get)

    result = music_links.resolve_links("https://open.spotify.com/track/x", country="GB")

    assert captured["params"] == {"url": "https://open.spotify.com/track/x", "userCountry": "GB"}
    assert result["title"] == "T"
    assert result["links"][0]["url"] == "https://open.spotify.com/track/x"


def test_resolve_links_wraps_network_error(monkeypatch):
    import requests

    def _boom(*a, **k):
        raise requests.RequestException("network down")

    monkeypatch.setattr(requests, "get", _boom)

    with pytest.raises(music_links.MusicResolveError):
        music_links.resolve_links("https://open.spotify.com/track/x")
