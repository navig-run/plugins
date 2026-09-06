"""Voice lookup and cloning — the bridge between a speaker name and a real voice.

A scenario refers to speakers by name (``NOBI``, ``SERIO``); the API refers to voices by
opaque id. These helpers are what turns one into the other: listing what the key can
use, and creating a clone from your own recordings so the id in the frontmatter is
actually your voice.

Cloning is a paid-plan feature. That is worth surfacing plainly rather than letting a
401 arrive with no explanation, so :func:`describe_capability` reports it from the
account rather than guessing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from navig.tools.audio_generation import AudioGenerator

# What ElevenLabs will accept as a cloning sample.
SAMPLE_SUFFIXES = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".webm", ".mp4"}


@dataclass(frozen=True)
class Voice:
    voice_id: str
    name: str
    category: str
    labels: dict[str, Any]

    @property
    def is_mine(self) -> bool:
        """True for voices you created — clones and designed voices, not the stock library."""
        return self.category in {"cloned", "professional", "generated"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "voice_id": self.voice_id,
            "name": self.name,
            "category": self.category,
            "mine": self.is_mine,
            "labels": self.labels,
        }


def _to_voice(payload: dict[str, Any]) -> Voice:
    return Voice(
        voice_id=str(payload.get("voice_id") or ""),
        name=str(payload.get("name") or "(unnamed)"),
        category=str(payload.get("category") or "premade"),
        labels=payload.get("labels") or {},
    )


async def list_voices(mine_only: bool = False) -> list[Voice]:
    """Every voice the configured key can speak with.

    Sorted so your own voices come first — with a stock library of dozens, the one you
    cloned is otherwise buried.
    """
    gen = AudioGenerator()
    try:
        raw = await gen.list_voices()
    finally:
        await gen.close()
    voices = [_to_voice(v) for v in raw]
    if mine_only:
        voices = [v for v in voices if v.is_mine]
    return sorted(voices, key=lambda v: (not v.is_mine, v.name.lower()))


def collect_samples(paths: list[Path] | list[str]) -> list[Path]:
    """Expand the given paths into a list of usable audio samples.

    Directories are walked so ``navig audio clone "Me" ./samples`` works, and unknown
    file types are dropped rather than uploaded — the API rejects them with a message
    that does not name the offending file.
    """
    found: list[Path] = []
    for entry in paths:
        path = Path(entry)
        if path.is_dir():
            found.extend(
                sorted(p for p in path.rglob("*") if p.suffix.lower() in SAMPLE_SUFFIXES)
            )
        elif path.suffix.lower() in SAMPLE_SUFFIXES:
            found.append(path)
    # Deduplicate while keeping order — a directory and a file inside it can both be given.
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in found:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique


async def clone(
    name: str,
    samples: list[Path],
    *,
    description: str | None = None,
    remove_background_noise: bool = True,
) -> dict[str, Any]:
    """Create an instant voice clone. Returns the API payload, including ``voice_id``."""
    gen = AudioGenerator()
    try:
        return await gen.create_instant_voice_clone(
            name,
            samples,
            description=description,
            remove_background_noise=remove_background_noise,
        )
    finally:
        await gen.close()


async def account() -> dict[str, Any]:
    """The raw subscription payload for the configured key."""
    gen = AudioGenerator()
    try:
        return await gen.subscription()
    finally:
        await gen.close()


def describe_capability(subscription: dict[str, Any]) -> str:
    """One line on what this plan can do about voices, in plain terms."""
    tier = str(subscription.get("tier") or "unknown")
    if subscription.get("can_use_professional_voice_cloning"):
        return f"{tier}: professional voice cloning available (highest fidelity)"
    if subscription.get("can_use_instant_voice_cloning"):
        return f"{tier}: instant voice cloning available — enough to podcast in your own voice"
    return (
        f"{tier}: no voice cloning on this plan, and no commercial licence. "
        f"Stock voices work for testing; using your own voice needs Starter or above."
    )
