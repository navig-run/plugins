"""Cross-platform music-link resolver (song.link / Odesli).

Given a share URL from one music service (Spotify, Apple Music, YouTube Music,
Deezer, Tidal, SoundCloud, Amazon Music, Bandcamp, …), resolve the *same* track
on every other platform via the free, keyless **song.link (Odesli)** API.

Migrated into navig-download (2026-07) from the retired ``telegram-bot-navig``
pack — the one capability in that pack with no native home. Kept as pure logic
(no transport deps) so the CLI, the Telegram channel, and the agent can all reuse
it: ``find_music_url()`` for passive auto-detect, ``resolve_links()`` for lookup.

API: https://api.song.link/v1-alpha.1/links  (free, no API key required).
"""

from __future__ import annotations

import re
from typing import Any

_ODESLI_ENDPOINT = "https://api.song.link/v1-alpha.1/links"

# Recognised music-service share URLs — used for passive auto-detect in chat
# surfaces (paste a link, get the cross-platform list back).
MUSIC_URL_RE = re.compile(
    r"https?://(?:open\.spotify\.com|music\.apple\.com|music\.youtube\.com"
    r"|(?:www\.)?deezer\.com|(?:listen\.)?tidal\.com|soundcloud\.com"
    r"|music\.amazon\.(?:com|co\.uk)|www\.last\.fm"
    r"|(?:[\w-]+\.)?bandcamp\.com|napster\.com|pandora\.com)\S+",
    re.IGNORECASE,
)

# song.link platform keys → human labels. Unknown keys fall back to Title-case.
PLATFORM_LABELS: dict[str, str] = {
    "spotify": "Spotify",
    "itunes": "Apple Music",
    "appleMusic": "Apple Music",
    "youtubeMusic": "YouTube Music",
    "youtube": "YouTube",
    "deezer": "Deezer",
    "tidal": "Tidal",
    "soundcloud": "SoundCloud",
    "amazonMusic": "Amazon Music",
    "amazonStore": "Amazon Store",
    "pandora": "Pandora",
    "napster": "Napster",
    "yandex": "Yandex Music",
    "bandcamp": "Bandcamp",
    "anghami": "Anghami",
    "boomplay": "Boomplay",
    "audius": "Audius",
    "spinrilla": "Spinrilla",
}


class MusicResolveError(RuntimeError):
    """The song.link lookup failed, or returned nothing usable."""


def find_music_url(text: str) -> str | None:
    """Return the first recognised music-service URL in *text*, or ``None``."""
    match = MUSIC_URL_RE.search(text or "")
    return match.group(0) if match else None


def _label_for(platform: str) -> str:
    return PLATFORM_LABELS.get(platform) or (platform[:1].upper() + platform[1:])


def parse_odesli(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize a raw song.link response into a stable shape.

    Returns ``{"title", "artist", "thumbnail", "page_url", "links": [...]}`` where
    each link is ``{"platform", "label", "url"}``. Raises :class:`MusicResolveError`
    when the payload carries no usable platform links.
    """
    entities = data.get("entitiesByUniqueId") or {}
    entity_id = data.get("entityUniqueId") or ""
    entity = entities.get(entity_id, {}) if entity_id else {}

    links: list[dict[str, str]] = []
    for platform, info in (data.get("linksByPlatform") or {}).items():
        url = (info or {}).get("url")
        if url:
            links.append({"platform": platform, "label": _label_for(platform), "url": url})

    if not links:
        raise MusicResolveError("no platform links found for this track")

    return {
        "title": entity.get("title"),
        "artist": entity.get("artistName"),
        "thumbnail": entity.get("thumbnailUrl"),
        "page_url": data.get("pageUrl"),
        "links": links,
    }


def resolve_links(url: str, *, country: str = "US", timeout: float = 15.0) -> dict[str, Any]:
    """Resolve *url* to the same track across every platform via song.link.

    *country* is a two-letter market code (Odesli ``userCountry``) that decides
    which store links come back. Raises :class:`MusicResolveError` on any failure.
    """
    import requests  # bundled dependency of navig-download

    try:
        resp = requests.get(
            _ODESLI_ENDPOINT,
            params={"url": url, "userCountry": country},
            timeout=timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
    except MusicResolveError:
        raise
    except Exception as exc:  # noqa: BLE001 — surface any network/parse error uniformly
        raise MusicResolveError(f"song.link lookup failed: {exc}") from exc

    return parse_odesli(payload)
