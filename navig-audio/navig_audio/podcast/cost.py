"""What an episode will cost, before it costs it.

ElevenLabs bills per character, and a podcast is a lot of characters: a 45-minute
episode is roughly 38,000 of them, and rendering it in two languages doubles that. On a
plan with 30,000 credits a month, an episode you were casually experimenting with can
consume the month. That is a bad thing to discover halfway through a render.

So estimation is a first-class step, not a footnote. Counting is done on the parsed
episode, which means it counts exactly what will be sent: speech only, with production
notes, bullets and cue lines already excluded by the parser.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from navig_audio.podcast.chunker import DEFAULT_MAX_CHARS, chunk_count
from navig_audio.podcast.scenario import Episode

# Credits charged per character of input, by model. Flash is billed at half rate; the
# quality models are billed one-for-one.
_MODEL_RATES: dict[str, float] = {
    "eleven_flash_v2": 0.5,
    "eleven_flash_v2_5": 0.5,
    "eleven_turbo_v2": 0.5,
    "eleven_turbo_v2_5": 0.5,
}
DEFAULT_RATE = 1.0

# Speech runs at roughly 900-1000 characters per minute of finished audio. Used only to
# turn a character count into a duration a human can sanity-check against.
CHARS_PER_MINUTE = 950


def credit_rate(model: str) -> float:
    """Credits per character for ``model``."""
    return _MODEL_RATES.get(model, DEFAULT_RATE)


@dataclass
class TrackCost:
    number: int
    title: str
    chars: int
    chunks: int
    speakers: list[str] = field(default_factory=list)

    @property
    def credits(self) -> int:
        return self.chars  # scaled by the model rate at the episode level


@dataclass
class Estimate:
    """Projected cost for one episode across one or more languages."""

    episode_title: str
    model: str
    languages: list[str]
    tracks: list[TrackCost]

    @property
    def chars_per_language(self) -> int:
        return sum(t.chars for t in self.tracks)

    @property
    def chunks_per_language(self) -> int:
        return sum(t.chunks for t in self.tracks)

    @property
    def total_chars(self) -> int:
        return self.chars_per_language * len(self.languages)

    @property
    def total_credits(self) -> int:
        return round(self.total_chars * credit_rate(self.model))

    @property
    def total_requests(self) -> int:
        return self.chunks_per_language * len(self.languages)

    @property
    def estimated_minutes(self) -> float:
        """Rough finished runtime per language, for a sanity check against the script."""
        return self.chars_per_language / CHARS_PER_MINUTE

    def to_dict(self) -> dict[str, Any]:
        return {
            "episode": self.episode_title,
            "model": self.model,
            "languages": self.languages,
            "credit_rate": credit_rate(self.model),
            "chars_per_language": self.chars_per_language,
            "total_chars": self.total_chars,
            "total_credits": self.total_credits,
            "total_requests": self.total_requests,
            "estimated_minutes_per_language": round(self.estimated_minutes, 1),
            "tracks": [
                {
                    "number": t.number,
                    "title": t.title,
                    "chars": t.chars,
                    "chunks": t.chunks,
                    "speakers": t.speakers,
                }
                for t in self.tracks
            ],
        }


def estimate(
    episode: Episode,
    languages: list[str] | None = None,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> Estimate:
    """Project the cost of rendering ``episode`` into ``languages``."""
    langs = [str(entry).lower() for entry in (languages or [episode.lang])]
    return Estimate(
        episode_title=episode.title,
        model=episode.model,
        languages=langs,
        tracks=[
            TrackCost(
                number=track.number,
                title=track.title,
                chars=track.billable_chars,
                chunks=chunk_count(track, max_chars),
                speakers=track.speakers,
            )
            for track in episode.tracks
        ],
    )


@dataclass
class Balance:
    """What the configured key is actually allowed and able to do."""

    tier: str
    used: int
    limit: int
    can_clone_instant: bool
    can_clone_professional: bool
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def remaining(self) -> int:
        return max(self.limit - self.used, 0)

    def covers(self, credits: int) -> bool:
        return self.remaining >= credits

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "used": self.used,
            "limit": self.limit,
            "remaining": self.remaining,
            "can_clone_instant": self.can_clone_instant,
            "can_clone_professional": self.can_clone_professional,
        }


def parse_balance(payload: dict[str, Any]) -> Balance:
    """Read the subscription payload into the few fields that drive decisions."""
    return Balance(
        tier=str(payload.get("tier") or "unknown"),
        used=int(payload.get("character_count") or 0),
        limit=int(payload.get("character_limit") or 0),
        can_clone_instant=bool(payload.get("can_use_instant_voice_cloning")),
        can_clone_professional=bool(payload.get("can_use_professional_voice_cloning")),
        raw=payload,
    )


def shortfall_message(estimate_: Estimate, balance: Balance) -> str | None:
    """A warning when the render will not fit in the remaining balance, else ``None``.

    Deliberately a warning and not a refusal: a partial render is still useful, the
    per-track cache means the spent credits are not wasted, and it is not this tool's
    place to decide the user cannot buy more.
    """
    needed = estimate_.total_credits
    if balance.covers(needed):
        return None
    return (
        f"this render needs about {needed:,} credits but only {balance.remaining:,} "
        f"remain on the {balance.tier} plan — it will stop partway. Rendered tracks are "
        f"cached, so topping up and re-running resumes rather than restarts."
    )
