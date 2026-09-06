"""Content-addressed cache for generated speech — so re-running is free.

Rendering an episode is the expensive operation in this whole pipeline, and the normal
workflow re-runs it constantly: fix a line, re-render; adjust a voice setting,
re-render; add the English version, re-render. Paying full price each time would make
iteration something you avoid, which is the opposite of what a tool should do.

Every clip is therefore keyed by everything that determines its sound. If nothing that
matters changed, the bytes come off disk and no credits are spent.

**Context is part of the key.** Two chunks with identical text but different
surrounding lines really do sound different — that conditioning is the whole reason
:mod:`navig_audio.podcast.chunker` passes it — so treating them as the same clip would
serve audio that no longer matches its neighbours. Editing a line therefore re-renders
its immediate neighbours too. That is a handful of chunks, and it is the honest answer.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from navig.platform.paths import cache_dir


def podcast_cache_dir() -> Path:
    """Where generated clips live. Honours NAVIG's usual cache location."""
    return cache_dir() / "podcast"


def clip_key(
    *,
    text: str,
    voice_id: str,
    model: str,
    output_format: str,
    settings: dict[str, Any] | None = None,
    previous_text: str | None = None,
    next_text: str | None = None,
) -> str:
    """A stable hash of everything that affects how this clip sounds."""
    payload = json.dumps(
        {
            "text": text,
            "voice_id": voice_id,
            "model": model,
            "output_format": output_format,
            "settings": settings or {},
            "previous_text": previous_text,
            "next_text": next_text,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CachedClip:
    """A clip on disk, with its timing sidecar if one was stored."""

    audio: Path
    alignment: Path

    @property
    def exists(self) -> bool:
        return self.audio.exists() and self.audio.stat().st_size > 0

    def load_alignment(self) -> dict[str, Any] | None:
        if not self.alignment.exists():
            return None
        try:
            return json.loads(self.alignment.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A corrupt sidecar means we re-render this clip; it never means we serve
            # subtitles that do not match the audio.
            return None


def locate(key: str, *, root: Path | None = None) -> CachedClip:
    """Where a clip with this key would live (whether or not it is there yet)."""
    base = (root or podcast_cache_dir()) / key[:2]
    return CachedClip(audio=base / f"{key}.mp3", alignment=base / f"{key}.json")


def store(
    key: str,
    audio: bytes,
    alignment: dict[str, Any] | None = None,
    *,
    root: Path | None = None,
) -> CachedClip:
    """Write a clip and its timings into the cache atomically.

    Written to a temporary name and renamed, because an interrupted render must not
    leave a half-file that later looks like a valid cache hit.
    """
    clip = locate(key, root=root)
    clip.audio.parent.mkdir(parents=True, exist_ok=True)

    tmp = clip.audio.with_suffix(".mp3.part")
    tmp.write_bytes(audio)
    tmp.replace(clip.audio)

    if alignment is not None:
        tmp_json = clip.alignment.with_suffix(".json.part")
        tmp_json.write_text(
            json.dumps(alignment, ensure_ascii=False), encoding="utf-8"
        )
        tmp_json.replace(clip.alignment)

    return clip


def stats(root: Path | None = None) -> dict[str, Any]:
    """Size and count of the cache, for `navig audio check`."""
    base = root or podcast_cache_dir()
    if not base.exists():
        return {"path": str(base), "clips": 0, "bytes": 0}
    clips = list(base.rglob("*.mp3"))
    return {
        "path": str(base),
        "clips": len(clips),
        "bytes": sum(p.stat().st_size for p in clips),
    }
