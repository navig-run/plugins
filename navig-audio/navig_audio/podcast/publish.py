"""Publishing to NobiCast — the contract, and what has to change before it can run.

This module deliberately does **not** upload anything yet. It reads and validates the
``metadata.json`` a render produces, so the contract is fixed and testable now, and the
upload becomes a small addition later rather than a redesign.

The hold-up is on the NobiCast side, and it is structural rather than cosmetic. As it
stands the app can store exactly one audio file per episode:

* ``episode_assets`` has primary key ``(episode_id, kind)``, so one ``audio`` row per
  episode — an album of tracks has nowhere to go.
* the R2 key is derived as ``audio/ep{number}.{ext}``, so a second track or a second
  language would overwrite the first.
* there is no ``lang`` column anywhere, and the web app is hardcoded French.
* there is no ``duration`` column; the RSS feed emits ``0:00:00`` for every episode.

What it needs is an ``episode_tracks`` table keyed by ``(episode_id, lang, track_no)``
and a key scheme like ``audio/ep{number}/{lang}/{NN}-{slug}.mp3``. Until that exists,
:func:`plan_upload` reports exactly what would be sent so the shape can be reviewed
before anything is written to a live bucket.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# The key scheme the NobiCast side will need, once it can hold more than one file.
R2_KEY_TEMPLATE = "audio/ep{number}/{lang}/{stem}.mp3"

# nobicast-web caps uploads at 50 MB per audio file.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


class PublishError(RuntimeError):
    """The rendered episode cannot be published as it stands."""


@dataclass
class PlannedUpload:
    lang: str
    track_no: int
    title: str
    local: Path
    r2_key: str
    duration_s: float
    bytes: int

    @property
    def too_large(self) -> bool:
        return self.bytes > MAX_UPLOAD_BYTES

    def to_dict(self) -> dict[str, Any]:
        return {
            "lang": self.lang,
            "track_no": self.track_no,
            "title": self.title,
            "local": str(self.local),
            "r2_key": self.r2_key,
            "duration_s": round(self.duration_s, 3),
            "bytes": self.bytes,
            "too_large": self.too_large,
        }


@dataclass
class UploadPlan:
    episode: int | None
    slug: str
    title: str
    uploads: list[PlannedUpload] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def languages(self) -> list[str]:
        return sorted({u.lang for u in self.uploads})

    def to_dict(self) -> dict[str, Any]:
        return {
            "episode": self.episode,
            "slug": self.slug,
            "title": self.title,
            "languages": self.languages,
            "uploads": [u.to_dict() for u in self.uploads],
            "warnings": self.warnings,
        }


def load_metadata(episode_dir: Path) -> dict[str, Any]:
    """Read the metadata contract written by a render."""
    path = Path(episode_dir) / "metadata.json"
    if not path.exists():
        raise PublishError(
            f"no metadata.json in {episode_dir} — render the episode first "
            f"(`navig audio render <scenario.md>`)"
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PublishError(f"metadata.json is not valid JSON: {exc}") from exc


def plan_upload(episode_dir: Path, *, languages: list[str] | None = None) -> UploadPlan:
    """Work out exactly what would be uploaded, without uploading it."""
    episode_dir = Path(episode_dir)
    meta = load_metadata(episode_dir)

    number = meta.get("episode")
    plan = UploadPlan(
        episode=int(number) if number is not None else None,
        slug=str(meta.get("slug") or ""),
        title=str(meta.get("title") or ""),
    )
    if plan.episode is None:
        plan.warnings.append(
            "the scenario has no `episode:` number, and the R2 key scheme is built from "
            "it — add one to the frontmatter before publishing"
        )

    wanted = {lang.lower() for lang in languages} if languages else None
    for lang, payload in (meta.get("languages") or {}).items():
        if wanted and lang.lower() not in wanted:
            continue
        for track in payload.get("tracks", []):
            local = episode_dir / lang / str(track.get("file") or "")
            if not local.exists():
                plan.warnings.append(f"missing rendered file: {local}")
                continue
            upload = PlannedUpload(
                lang=lang,
                track_no=int(track.get("number") or 0),
                title=str(track.get("title") or ""),
                local=local,
                r2_key=R2_KEY_TEMPLATE.format(
                    number=plan.episode if plan.episode is not None else "x",
                    lang=lang,
                    stem=local.stem,
                ),
                duration_s=float(track.get("duration_s") or 0.0),
                bytes=int(track.get("bytes") or local.stat().st_size),
            )
            if upload.too_large:
                plan.warnings.append(
                    f"{local.name} is {upload.bytes / 1_048_576:.1f} MB, over the "
                    f"50 MB upload limit"
                )
            plan.uploads.append(upload)

    if not plan.uploads:
        raise PublishError(
            "nothing to publish — no rendered tracks found in "
            f"{episode_dir} for {', '.join(sorted(wanted)) if wanted else 'any language'}"
        )
    plan.uploads.sort(key=lambda u: (u.lang, u.track_no))
    return plan


def blockers() -> list[str]:
    """What NobiCast needs before an upload can actually happen."""
    return [
        "episode_assets has PK (episode_id, kind) — one audio row per episode; an album "
        "needs an episode_tracks table keyed (episode_id, lang, track_no)",
        "R2 keys are derived as audio/ep{number}.{ext} and would collide across tracks "
        "and languages; needs " + R2_KEY_TEMPLATE,
        "no lang column on episodes, and the web app is hardcoded French",
        "no duration column; the RSS feed hardcodes <itunes:duration>0:00:00",
    ]
