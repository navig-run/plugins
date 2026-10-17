"""ElevenLabs library surface — browse and pull what the account already has.

Three read-only views of the same subscription, plus their downloads:

history      every past generation (list → fetch its audio → batch-download a zip)
shared-voices the public voice library (search by language / gender / use case …)
assets       uploaded static assets (the Pro-only reference store; a creator key
             gets the honest 402 rather than an empty list)

Nothing here GENERATES: these are GETs plus one zip download, so browsing and
fetching cost no credits. The plan/credit view itself lives on
``AudioGenerator.subscription()``; this module is only the library.

Errors reuse ``audio_generation``'s checker deliberately: same provider, same
shape of failure (the body carries "This endpoint requires a Pro plan or
above." for the gated calls), one message format across the whole plugin.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import httpx

    HTTPX_AVAILABLE = True
except ImportError:
    httpx = None  # type: ignore[assignment]
    HTTPX_AVAILABLE = False

from navig_media_core.tools.media_providers import resolve_media_key

from navig_generate._compat import get_logger
from navig_generate.tools.audio_generation import _ELEVEN_BASE, AudioGenerationError, _check

logger = get_logger("tools.media_library")

# The API's own page cap for history; asking for more is silently clamped, so
# pass through what the caller asked for but document the real ceiling.
_MAX_PAGE_SIZE = 100


class MediaLibrary:
    """Read-only browsing of an ElevenLabs account's produced media and voices."""

    def __init__(self) -> None:
        if not HTTPX_AVAILABLE:
            raise ImportError("httpx is required for the media library. Install: pip install httpx")
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=120.0)
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    def _api_key(self) -> str:
        key = resolve_media_key("elevenlabs", "ELEVENLABS_API_KEY", "ELEVEN_API_KEY")
        if not key:
            raise ValueError("ElevenLabs API key not configured")
        return key

    # ── History — every generation this key has made ──────────────────────────

    async def history(
        self,
        *,
        page_size: int = 30,
        start_after_history_item_id: str | None = None,
        voice_id: str | None = None,
        model_id: str | None = None,
        search: str | None = None,
        source: str | None = None,
        date_before_unix: int | None = None,
        date_after_unix: int | None = None,
        sort_direction: str | None = None,
    ) -> dict[str, Any]:
        """One page of history, newest first. Returns ``{history, has_more}``.

        Paging is cursor-style: pass the last item's ``history_item_id`` as
        ``start_after_history_item_id`` for the next page (the API's own
        mechanism — offset paging would skip/duplicate on a live account).
        """
        client = await self._get_client()
        params: dict[str, Any] = {"page_size": min(max(page_size, 1), _MAX_PAGE_SIZE)}
        for key, val in (
            ("start_after_history_item_id", start_after_history_item_id),
            ("voice_id", voice_id),
            ("model_id", model_id),
            ("search", search),
            ("source", source),
            ("date_before_unix", date_before_unix),
            ("date_after_unix", date_after_unix),
            ("sort_direction", sort_direction),
        ):
            if val is not None:
                params[key] = val
        resp = await client.get(
            f"{_ELEVEN_BASE}/history",
            params=params,
            headers={"xi-api-key": self._api_key()},
        )
        _check(resp, "history listing")
        payload = resp.json()
        return payload if isinstance(payload, dict) else {}

    async def history_item(self, history_item_id: str) -> dict[str, Any]:
        """One history record with its full metadata (text, settings, timings)."""
        client = await self._get_client()
        resp = await client.get(
            f"{_ELEVEN_BASE}/history/{history_item_id}",
            headers={"xi-api-key": self._api_key()},
        )
        _check(resp, "history item")
        payload = resp.json()
        return payload if isinstance(payload, dict) else {}

    async def history_audio(self, history_item_id: str) -> bytes:
        """Download the audio of one history item (``audio/mpeg`` bytes)."""
        client = await self._get_client()
        resp = await client.get(
            f"{_ELEVEN_BASE}/history/{history_item_id}/audio",
            headers={"xi-api-key": self._api_key()},
        )
        _check(resp, "history audio")
        return resp.content

    async def history_download(self, history_item_ids: list[str]) -> bytes:
        """Batch-download history items as the provider's zip archive.

        One request for many items — the intended way to pull a day's output
        instead of N per-item fetches. Returns the zip bytes.
        """
        if not history_item_ids:
            raise AudioGenerationError("history_download needs at least one item id")
        client = await self._get_client()
        resp = await client.post(
            f"{_ELEVEN_BASE}/history/download",
            headers={"xi-api-key": self._api_key(), "Content-Type": "application/json"},
            json={"history_item_ids": list(history_item_ids)},
        )
        _check(resp, "history download")
        return resp.content

    # ── Voice library — where a new voice comes from ──────────────────────────

    async def shared_voices(
        self,
        *,
        search: str | None = None,
        page_size: int = 30,
        page: int | None = None,
        gender: str | None = None,
        age: str | None = None,
        accent: str | None = None,
        language: str | None = None,
        locale: str | None = None,
        category: str | None = None,
        use_cases: str | None = None,
        sort: str | None = None,
    ) -> dict[str, Any]:
        """Search the public voice library. Returns ``{voices, has_more?}``.

        Each entry carries ``preview_url`` (play before adopting) and
        ``voice_id`` (adopt with ``voices/add/{public_owner_id}/{voice_id}``), so
        a search here is the front door to adding a voice to the account.
        """
        client = await self._get_client()
        params: dict[str, Any] = {"page_size": min(max(page_size, 1), 100)}
        for key, val in (
            ("search", search), ("page", page), ("gender", gender), ("age", age),
            ("accent", accent), ("language", language), ("locale", locale),
            ("category", category), ("use_cases", use_cases), ("sort", sort),
        ):
            if val is not None:
                params[key] = val
        resp = await client.get(
            f"{_ELEVEN_BASE}/shared-voices",
            params=params,
            headers={"xi-api-key": self._api_key()},
        )
        _check(resp, "voice library search")
        payload = resp.json()
        return payload if isinstance(payload, dict) else {}

    # ── Assets — the uploaded reference store (Pro+) ──────────────────────────

    async def assets(
        self, *, page_size: int = 50, cursor: str | None = None, search: str | None = None
    ) -> dict[str, Any]:
        """List uploaded assets. Returns the provider payload as-is.

        Plan-gated: a creator-tier key gets 402 ``paid_plan_required`` (measured
        2026-10-14), raised verbatim by ``_check`` so the message says which plan
        is needed instead of an empty list pretending there are no assets.
        """
        client = await self._get_client()
        params: dict[str, Any] = {"page_size": min(max(page_size, 1), 100)}
        if cursor:
            params["cursor"] = cursor
        if search:
            params["search"] = search
        resp = await client.get(
            f"{_ELEVEN_BASE}/assets",
            params=params,
            headers={"xi-api-key": self._api_key()},
        )
        _check(resp, "asset listing")
        payload = resp.json()
        return payload if isinstance(payload, dict) else {}


def write_output_file(blob: bytes, dest: str | Path, *, label: str = "output") -> Path:
    """Write a provider-produced artifact to ``dest`` and return the path.

    The ONE writer for tool output (history fetch, isolate, change-voice, stems):
    empty output is refused — a 0-byte file from a 200-with-empty-body would be
    silently unplayable downstream, and a success line over one lies about what
    happened. Named for what it is (bytes → file), not for one caller.
    """
    if not blob:
        raise AudioGenerationError(f"{label}: provider returned no bytes")
    out = Path(dest).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(blob)
    return out
