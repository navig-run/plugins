"""Rendering an episode: scenario in, an album of finished tracks out.

The shape of the output is the design decision that matters. An episode is **not** one
long file — it is a numbered set of tracks, the way an album is, because that is how
these scripts are written and because it makes every subsequent operation cheaper:

* a bad segment is re-rendered on its own instead of re-buying the whole episode;
* each track stays comfortably inside the model's per-request limits;
* NobiCast can serve, reorder or replace one segment without touching the rest.

A stitched master is produced as well, since some things (a podcast RSS feed) still
want a single file, but it is derived from the tracks rather than the other way round.

Everything is cached by content, so re-running after an edit only pays for what changed.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from navig.core.json_io import atomic_write_json, load_json_for_update
from navig.media.audio_edit import (
    AudioEditError,
    concat,
    ffmpeg_available,
    normalize,
    probe_duration,
)
from navig.tools.audio_generation import AudioGenerationConfig, AudioGenerator
from navig_audio.podcast import cache, srt
from navig_audio.podcast.chunker import DEFAULT_MAX_CHARS, Chunk, plan_track
from navig_audio.podcast.scenario import Cue, Episode, Track

# Silence between album tracks, so segments do not run into each other.
TRACK_GAP_S = 0.6

Progress = Callable[[str], None]


class RenderError(RuntimeError):
    """Rendering could not complete. Always names what to fix."""


@dataclass
class RenderedTrack:
    number: int
    title: str
    slug: str
    path: Path
    duration_s: float
    chars: int
    generated: int
    cached: int
    generated_chars: int = 0
    subtitles: Path | None = None
    transcript: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "title": self.title,
            "slug": self.slug,
            "file": self.path.name,
            "duration_s": round(self.duration_s, 3),
            "chars": self.chars,
            "generated_chunks": self.generated,
            "cached_chunks": self.cached,
            "generated_chars": self.generated_chars,
            "subtitles": self.subtitles.name if self.subtitles else None,
            "bytes": self.path.stat().st_size if self.path.exists() else 0,
        }


@dataclass
class RenderResult:
    episode: str
    lang: str
    directory: Path
    tracks: list[RenderedTrack] = field(default_factory=list)
    master: Path | None = None
    master_subtitles: Path | None = None
    credits_spent: int = 0

    @property
    def duration_s(self) -> float:
        return sum(t.duration_s for t in self.tracks) + TRACK_GAP_S * max(len(self.tracks) - 1, 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            # "title", not "episode": the enclosing metadata.json already uses `episode`
            # for the number, and two keys meaning different things under one name is
            # exactly the ambiguity a publish step would trip on.
            "title": self.episode,
            "lang": self.lang,
            "directory": str(self.directory),
            "duration_s": round(self.duration_s, 3),
            "credits_spent": self.credits_spent,
            "master": self.master.name if self.master else None,
            "master_subtitles": self.master_subtitles.name if self.master_subtitles else None,
            "tracks": [t.to_dict() for t in self.tracks],
        }


def _noop(_: str) -> None:
    return None


async def _clip_for_chunk(
    gen: AudioGenerator,
    episode: Episode,
    chunk: Chunk,
    *,
    output_format: str,
    request_ids: list[str],
    progress: Progress,
) -> tuple[Path, dict[str, Any], bool]:
    """Ensure this chunk exists on disk. Returns (path, alignment, was_generated)."""
    voice = episode.voice_for(chunk.speaker)
    if not voice.voice_id:
        raise RenderError(
            f"speaker {chunk.speaker!r} has no `voice_id` in the frontmatter — "
            f"add one (list yours with `navig audio voices`) before rendering"
        )

    key = cache.clip_key(
        text=chunk.text,
        voice_id=voice.voice_id,
        model=episode.model,
        output_format=output_format,
        settings=voice.settings,
        previous_text=chunk.previous_text,
        next_text=chunk.next_text,
    )
    clip = cache.locate(key)
    if clip.exists:
        alignment = clip.load_alignment()
        if alignment is not None:
            return clip.audio, alignment, False

    progress(f"    generating {chunk.speaker} · {chunk.chars} chars")
    timed = await gen.tts_with_timestamps(
        chunk.text,
        voice.voice_id,
        model_id=episode.model,
        voice_settings=voice.settings or None,
        language_code=episode.lang,
        previous_text=chunk.previous_text,
        next_text=chunk.next_text,
        previous_request_ids=request_ids or None,
    )
    if not timed.audio:
        raise RenderError(
            f"the provider returned no audio for track chunk {chunk.index} "
            f"({chunk.chars} chars, voice {voice.voice_id})"
        )
    alignment = {
        "characters": timed.characters,
        "starts": timed.starts,
        "ends": timed.ends,
        "request_id": timed.request_id,
    }
    cache.store(key, timed.audio, alignment)
    return cache.locate(key).audio, alignment, True


async def _clip_for_cue(
    gen: AudioGenerator, cue: Cue, *, progress: Progress
) -> tuple[Path, bool]:
    """Ensure a music/SFX cue exists on disk. Returns (path, was_generated)."""
    key = cache.clip_key(
        text=f"[{cue.kind}] {cue.prompt}",
        voice_id="-",
        model=cue.kind,
        output_format=f"{cue.duration_s or 0}",
    )
    clip = cache.locate(key)
    if clip.exists:
        return clip.audio, False

    progress(f"    generating {cue.kind} · {cue.prompt[:48]}")
    result = await gen.generate(
        cue.prompt, kind=cue.kind, duration_s=cue.duration_s, save=False
    )
    if not result.audio:
        raise RenderError(f"the provider returned no audio for the {cue.kind} cue {cue.prompt!r}")
    cache.store(key, result.audio)
    return cache.locate(key).audio, True


async def render_track(
    gen: AudioGenerator,
    episode: Episode,
    track: Track,
    out_dir: Path,
    *,
    output_format: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    progress: Progress = _noop,
) -> RenderedTrack:
    """Render one track to a single mp3, with subtitles and a transcript beside it."""
    progress(f"  track {track.number:02d} — {track.title}")
    plan = plan_track(track, max_chars)
    if not plan:
        raise RenderError(f"track {track.number} ({track.title!r}) has nothing to say")

    parts: list[Path] = []
    cues: list[srt.SubtitleCue] = []
    transcript: list[str] = []
    generated = cached = 0
    generated_chars = 0
    elapsed = 0.0
    last_speaker: str | None = None
    # Only the ids of clips generated in this run can be referenced; a cache hit did not
    # produce a request. Kept per speaker run, cleared whenever the thread of speech breaks.
    request_ids: list[str] = []

    for item in plan:
        if isinstance(item, Cue):
            path, was_new = await _clip_for_cue(gen, item, progress=progress)
            generated += int(was_new)
            cached += int(not was_new)
            request_ids.clear()
            last_speaker = None
        else:
            if last_speaker is not None and last_speaker != item.speaker:
                request_ids.clear()
            last_speaker = item.speaker
            path, alignment, was_new = await _clip_for_chunk(
                gen, episode, item,
                output_format=output_format, request_ids=request_ids, progress=progress,
            )
            generated += int(was_new)
            cached += int(not was_new)
            if was_new:
                generated_chars += item.chars
            rid = alignment.get("request_id")
            if was_new and rid:
                request_ids.append(rid)
                del request_ids[:-3]
            cues.extend(
                srt.cues_from_alignment(
                    alignment.get("characters", []),
                    alignment.get("starts", []),
                    alignment.get("ends", []),
                    offset=elapsed,
                    start_index=len(cues) + 1,
                )
            )
            transcript.append(f"{item.speaker}: {item.text}")

        parts.append(path)
        # Measured, not taken from the alignment: the alignment ends at the last spoken
        # character, while the file may carry a beat of silence after it. Using the
        # spoken end would pull every later subtitle progressively early.
        elapsed += probe_duration(path)

    stem = track.stem()
    raw = out_dir / f"{stem}.raw.mp3"
    final = out_dir / f"{stem}.mp3"
    try:
        concat(parts, raw)
        # Mastered per track, not only on the master, because the tracks are the
        # deliverable — they get played on their own.
        normalize(raw, final)
    except AudioEditError as exc:
        raise RenderError(f"track {track.number}: {exc}") from exc
    finally:
        raw.unlink(missing_ok=True)

    # Only write the text sidecars when there is text. A cue-only track (a sting, a
    # transition) has no speech, and an empty .srt is worse than none — a video tool
    # loads it and shows nothing instead of falling back.
    srt_path: Path | None = None
    txt_path: Path | None = None
    if cues:
        srt_path = out_dir / f"{stem}.srt"
        srt_path.write_text(srt.to_srt(srt.renumber(cues)), encoding="utf-8")
    if transcript:
        txt_path = out_dir / f"{stem}.txt"
        txt_path.write_text("\n\n".join(transcript) + "\n", encoding="utf-8")

    return RenderedTrack(
        number=track.number,
        title=track.title,
        slug=track.slug,
        path=final,
        duration_s=probe_duration(final),
        chars=track.billable_chars,
        generated=generated,
        cached=cached,
        generated_chars=generated_chars,
        subtitles=srt_path,
        transcript=txt_path,
    )


async def render_episode(
    episode: Episode,
    out_dir: Path,
    *,
    tracks: list[int] | None = None,
    master: bool = True,
    max_chars: int = DEFAULT_MAX_CHARS,
    progress: Progress = _noop,
) -> RenderResult:
    """Render ``episode`` into ``out_dir/<lang>/``.

    ``tracks`` limits the render to those track numbers; the master is skipped in that
    case, because stitching a master from a partly-rendered episode would quietly
    produce a file with segments missing.
    """
    if not ffmpeg_available():
        raise RenderError(
            "ffmpeg is not installed or not on PATH — it is needed to join and master "
            "the clips (scoop install ffmpeg / brew install ffmpeg / apt install ffmpeg)"
        )

    lang_dir = out_dir / episode.lang
    lang_dir.mkdir(parents=True, exist_ok=True)

    config = AudioGenerationConfig.from_env()
    config.tts_model = episode.model
    gen = AudioGenerator(config)

    selected = episode.tracks
    if tracks:
        selected = [episode.track(n) for n in tracks]

    result = RenderResult(episode=episode.title, lang=episode.lang, directory=lang_dir)
    try:
        for track in selected:
            rendered = await render_track(
                gen, episode, track, lang_dir,
                output_format=config.output_format, max_chars=max_chars, progress=progress,
            )
            result.tracks.append(rendered)
            result.credits_spent += _spent(rendered, episode)
    finally:
        await gen.close()

    if master and not tracks and len(result.tracks) > 1:
        result.master, result.master_subtitles = _build_master(result, lang_dir, progress)
    elif master and not tracks and len(result.tracks) == 1:
        result.master = result.tracks[0].path
        result.master_subtitles = result.tracks[0].subtitles

    _write_sidecars(episode, result, out_dir)
    return result


def _spent(rendered: RenderedTrack, episode: Episode) -> int:
    """Credits actually charged. Counts the characters really sent, not a share of the
    track — a cache hit costs nothing, so estimating by proportion would overstate it."""
    from navig_audio.podcast.cost import credit_rate

    return round(rendered.generated_chars * credit_rate(episode.model))


def _build_master(
    result: RenderResult, lang_dir: Path, progress: Progress
) -> tuple[Path | None, Path | None]:
    """Stitch the tracks into one file and shift every subtitle onto that timeline."""
    progress("  stitching master")
    master = lang_dir / "master.mp3"
    try:
        concat([t.path for t in result.tracks], master, gap_s=TRACK_GAP_S)
    except AudioEditError as exc:
        # The tracks are the deliverable; failing to glue them is not worth discarding
        # a successful render over.
        progress(f"  master could not be stitched: {exc}")
        return None, None

    merged: list[srt.SubtitleCue] = []
    offset = 0.0
    for index, track in enumerate(result.tracks):
        if track.subtitles and track.subtitles.exists():
            merged.extend(srt.shift(_read_cues(track.subtitles), offset))
        offset += track.duration_s + (TRACK_GAP_S if index < len(result.tracks) - 1 else 0)

    if not merged:
        return master, None
    master_srt = lang_dir / "master.srt"
    master_srt.write_text(srt.to_srt(srt.renumber(merged)), encoding="utf-8")
    return master, master_srt


def _read_cues(path: Path) -> list[srt.SubtitleCue]:
    """Parse back an SRT we wrote, so the master can re-time it."""
    cues: list[srt.SubtitleCue] = []
    blocks = [b for b in path.read_text(encoding="utf-8").split("\n\n") if b.strip()]
    for block in blocks:
        lines = block.strip().splitlines()
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        start_raw, end_raw = (part.strip() for part in lines[1].split("-->"))
        cues.append(
            srt.SubtitleCue(
                index=int(lines[0]),
                start=_parse_timestamp(start_raw),
                end=_parse_timestamp(end_raw),
                text="\n".join(lines[2:]),
            )
        )
    return cues


def _parse_timestamp(value: str) -> float:
    hours, minutes, rest = value.split(":")
    seconds, millis = rest.split(",")
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000


def _write_sidecars(episode: Episode, result: RenderResult, out_dir: Path) -> None:
    """Write chapters and the metadata contract the publish step will read."""
    chapters = []
    offset = 0.0
    for index, track in enumerate(result.tracks):
        chapters.append(
            {
                "number": track.number,
                "title": track.title,
                "start_s": round(offset, 3),
                "end_s": round(offset + track.duration_s, 3),
            }
        )
        offset += track.duration_s + (TRACK_GAP_S if index < len(result.tracks) - 1 else 0)

    (result.directory / "chapters.json").write_text(
        json.dumps(chapters, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # One metadata file per episode, merged across languages, so a later publish step
    # sees the whole release rather than one language at a time.
    meta_path = out_dir / "metadata.json"
    # This is a read-MODIFY-write across languages: rendering `en` merges into the entry
    # `fr` already wrote. Swallowing a failed read into `{}` here would therefore not
    # lose "nothing" — it would silently erase every other language from the release the
    # moment the file was locked or half-written. `load_json_for_update` raises instead,
    # so the save aborts with the old file intact, and the write is atomic so a crash
    # mid-write cannot leave the truncated file that would cause it next time.
    meta: dict[str, Any] = load_json_for_update(meta_path, default={})

    meta.update(
        {
            "episode": episode.number,
            "slug": episode.slug,
            "title": episode.title,
            "model": episode.model,
            "updated_at": datetime.now(UTC).isoformat(),
        }
    )
    meta.setdefault("languages", {})
    meta["languages"][episode.lang] = result.to_dict()
    atomic_write_json(meta, meta_path)
