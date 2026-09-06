"""Reels — the clip stage of the assembly line.

The scenario compiler in ``navig-audio`` already turns a script into narration with real
subtitle timings. This is the other half: capture the picture, cut it to the narration,
burn the captions in, and lay the score under it — one upload-ready vertical MP4 per
language, from one source scenario.

It reaches across plugins the way every other stage here does — through the same public
seams a user would (``navig_audio.podcast`` to narrate, ``navig.browser.cdp_actions`` to
capture, ``navig.media.video_edit`` to assemble) — and each is soft-imported, so a
missing piece drops its stage with a reason instead of breaking the chain.

**The load-bearing idea: picture is cut to the voice, never the other way round.**
A track's shots are scaled to exactly the duration its narration turned out to be, so a
cut can never land mid-sentence. That means an explicit ``secs=`` on a shot is a
*pacing ratio*, not an absolute — see :func:`plan_shots`, which says so out loud when it
has to override one by much.
"""

from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

_log = logging.getLogger(__name__)

Progress = Callable[[str], None]

# Below this, a shot is not a shot — it is a flash frame nobody can read.
MIN_SHOT_S = 0.35

# Which model generates a `prompt=` shot when the scenario does not say. Replicate,
# because one key reaches Kling / Wan / Hailuo / Luma and it is pay-per-use with no
# subscription — the lowest-commitment way to have generated footage work at all. The
# core client defaults to Veo instead, which is a fine default for a one-off `navig
# generate` call and a confusing one here: it would report a missing GOOGLE key while
# the docs and the error hint both talk about Replicate.
# Override per shot with `provider=`, or globally with VIDEO_PROVIDER.
DEFAULT_VIDEO_PROVIDER = "replicate"

# The core client defaults to Kling v2.1, which is one of the more expensive models on
# Replicate. A reel wants many short shots rather than one showpiece, so the cheap fast
# model is the right default here — measured at roughly a tenth the cost per second.
DEFAULT_REPLICATE_MODEL = "wan-video/wan-2.2-t2v-fast"

# Ask the model for vertical directly. The alternative — generating 16:9 and cropping to
# 9:16 — discards about 70% of the frame width, and with it the composition the model was
# asked for: subjects end up cropped out of their own shot.
# These keys match DEFAULT_REPLICATE_MODEL. A scenario naming a different model sets
# `video_input` in its frontmatter, which REPLACES this wholesale — an image-to-video
# model has no `aspect_ratio` at all, and Replicate rejects a key the model never declared.
VERTICAL_INPUT = {"aspect_ratio": "9:16", "resolution": "720p"}
# An `image=` with one of these extensions is a clip to reframe, not a still to hold.
FOOTAGE_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}
# How far a shot's declared pacing may be stretched before we say so.
PACING_WARN_RATIO = 0.15


class ReelError(RuntimeError):
    """The reel could not be built — bad scenario, missing capability, or a failed shot."""


def _installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):  # pragma: no cover - find_spec on a broken pkg
        return False


def detect_capabilities() -> dict[str, bool]:
    """Which parts of the reel stage can run right now."""
    caps = {
        "narrate": _installed("navig_audio"),
        "capture": _installed("navig.browser.cdp_actions"),
        "assemble": _installed("navig.media.video_edit"),
    }
    if caps["assemble"]:
        try:
            from navig.media.video_edit import ffmpeg_available

            caps["assemble"] = ffmpeg_available()
        except Exception:  # noqa: BLE001
            caps["assemble"] = False
    return caps


@dataclass
class PlannedShot:
    """One shot with the duration it will actually occupy on screen."""

    index: int
    seconds: float
    url: str | None = None
    image: str | None = None
    prompt: str | None = None
    provider: str | None = None
    motion: str = "none"

    @property
    def source(self) -> str:
        if self.url:
            return "capture"
        if self.prompt:
            return "generate"
        return "still"

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index, "seconds": round(self.seconds, 3),
            "source": self.source, "url": self.url, "image": self.image,
            "prompt": self.prompt, "provider": self.provider, "motion": self.motion,
        }


@dataclass
class ReelResult:
    lang: str
    path: Path | None = None
    duration_s: float = 0.0
    shots: int = 0
    captured: int = 0
    credits_spent: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lang": self.lang,
            "path": str(self.path) if self.path else None,
            "duration_s": round(self.duration_s, 3),
            "shots": self.shots,
            "captured": self.captured,
            "credits_spent": self.credits_spent,
            "notes": self.notes,
        }


def scenario_for(base: Path, lang: str) -> Path:
    """The scenario file for ``lang``, following the album's sibling convention.

    ``ep000.fr.md`` → ``ep000.en.md``; a file with no language infix gets one appended.
    """
    base = Path(base)
    stem = base.stem
    if "." in stem:
        head, _, tail = stem.rpartition(".")
        # A language tag is `fr` or `fr-CA` — NOT merely "short and alphabetic", which
        # also describes `ep000.draft.md` and would silently replace the wrong segment.
        is_tag = (len(tail) == 2 and tail.isalpha()) or (
            len(tail) == 5 and tail[2] == "-" and tail[:2].isalpha() and tail[3:].isalpha()
        )
        if is_tag:
            return base.with_name(f"{head}.{lang}{base.suffix}")
    return base.with_name(f"{stem}.{lang}{base.suffix}")


def plan_shots(
    shots: list[Any], audio_s: float, *, track: str = "",
    against: str = "narration", follows: str = "the voice", measured: str = "spoken",
) -> tuple[list[PlannedShot], str | None]:
    """Fit ``shots`` to exactly ``audio_s`` seconds of narration.

    ``against``, ``follows`` and ``measured`` only name what the picture is being cut to
    in the messages this produces. A clip is cut to a song, where "spoken" describes
    something that does not exist — and a warning naming the wrong thing is worse than no
    warning, because it sends the reader looking for narration to fix.

    Returns the planned shots and, when the author's pacing had to be stretched or
    squeezed by more than :data:`PACING_WARN_RATIO`, a note saying so.

    The scaling is the whole point. A shot written as ``secs=3.4`` is a statement about
    *relative* pacing — "this one is twice as long as that one" — because the absolute
    length is not knowable until the line has been spoken. Honouring the literal number
    instead would leave the picture cutting while the sentence is still running, which is
    the one artefact a viewer notices immediately.
    """
    if audio_s <= 0:
        raise ReelError(f"track {track or '?'} has no {against} to cut against")
    if not shots:
        raise ReelError(
            f"track {track or '?'} has no [shot: ...] — a reel needs picture for every "
            "track, or the video would go black while the line is spoken"
        )

    explicit = [s.secs for s in shots if getattr(s, "secs", None)]
    fallback = (sum(explicit) / len(explicit)) if explicit else 1.0
    weights = [float(getattr(s, "secs", None) or fallback) for s in shots]
    total = sum(weights)
    if total <= 0:  # pragma: no cover - every weight is positive by construction
        weights, total = [1.0] * len(shots), float(len(shots))

    seconds = [weight / total * audio_s for weight in weights]
    count = len(seconds)

    if MIN_SHOT_S * count >= audio_s:
        # More shots than the narration has room for. The floor cannot be honoured at
        # all, and sync outranks legibility — an even split is the least-bad answer.
        seconds = [audio_s / count] * count
    else:
        # Lift the too-short shots to the floor and take the time back from the others,
        # repeating because taking time back can push a new shot under the floor.
        # (Clamping and then renormalising everything, as the first version did, simply
        # scales the floored shots straight back below the floor.)
        pinned = [False] * count
        for _ in range(count):
            debt = 0.0
            for i in range(count):
                if not pinned[i] and seconds[i] < MIN_SHOT_S:
                    debt += MIN_SHOT_S - seconds[i]
                    seconds[i] = MIN_SHOT_S
                    pinned[i] = True
            if debt <= 0:
                break
            free = sum(seconds[i] for i in range(count) if not pinned[i])
            if free <= debt:  # pragma: no cover - the guard above makes this unreachable
                break
            shrink = (free - debt) / free
            for i in range(count):
                if not pinned[i]:
                    seconds[i] *= shrink

    planned = [
        PlannedShot(
            index=i,
            seconds=seconds[i],
            url=getattr(shot, "url", None),
            image=getattr(shot, "image", None),
            prompt=getattr(shot, "prompt", None),
            provider=getattr(shot, "provider", None),
            motion=getattr(shot, "motion", "none"),
        )
        for i, shot in enumerate(shots)
    ]

    note = None
    if explicit and len(explicit) == len(shots):
        ratio = audio_s / total
        if abs(ratio - 1.0) > PACING_WARN_RATIO:
            direction = "stretched" if ratio > 1 else "tightened"
            note = (
                f"track {track or '?'}: shots {direction} {abs(ratio - 1.0) * 100:.0f}% "
                f"({total:.1f}s written vs {audio_s:.1f}s {measured}) — picture follows "
                f"{follows}, so the written secs are treated as relative pacing"
            )
    return planned, note


# ── the build ─────────────────────────────────────────────────────────────────


async def _capture(
    shot: PlannedShot, dst: Path, *, port: int, width: int, height: int,
    fps: int, base_dir: Path, progress: Progress,
    style: Callable[[str], str] = lambda scene: scene,
    model: str | None = None,
    extra_input: dict[str, Any] | None = None,
    image_key: str | None = None,
) -> Path:
    """Realise one planned shot as a clip — generated, captured live, or built from a still.

    ``model`` overrides the Replicate model for a generated shot. It defaults to None rather
    than to the constant so a caller that does not care keeps getting the reel default, and
    a caller that does — a music clip wanting a showpiece model — need not restate it.
    """
    from navig.media.video_edit import fit_duration, still, to_vertical

    if shot.prompt:
        # AI-generated footage, for the beats that have no UI to film: the mythology, the
        # world, the before-and-after. The model decides the clip's own length, so it is
        # reframed and then cut to the narration like any other shot — the picture still
        # follows the voice.
        from navig.tools.video_generation import (
            VideoGenerationConfig,
            VideoGenerator,
            VideoProvider,
        )

        provider = VideoProvider(shot.provider or DEFAULT_VIDEO_PROVIDER)
        config = VideoGenerationConfig(provider=provider)
        inputs: dict[str, Any] | None = None
        if provider is VideoProvider.REPLICATE:
            config.replicate_model = model or DEFAULT_REPLICATE_MODEL
            if image_key:
                config.replicate_image_key = image_key
            # A scenario's own inputs REPLACE the vertical defaults rather than merging
            # with them, because merging cannot remove a key and removal is exactly what a
            # different model needs. Measured against the live schemas: wan-2.2-t2v-fast
            # takes `aspect_ratio`, wan-2.2-i2v-fast does not (its seed image settles the
            # aspect) — and Replicate 422s an input a model never declared. So a merge
            # would make it impossible to drive any image-to-video model at all.
            inputs = dict(extra_input) if extra_input else dict(VERTICAL_INPUT)
        generator = VideoGenerator(config)
        seed_url: str | None = None
        if shot.image:
            # A shot with BOTH `image=` and `prompt=` is image-to-video: this exact frame,
            # moved this way. It is the answer to a text-to-video model inventing its own
            # world — the art direction is already settled in the still, so the model is
            # asked to animate rather than to imagine.
            source = (base_dir / shot.image) if not Path(shot.image).is_absolute() else Path(shot.image)
            if not source.exists():
                raise ReelError(f"shot {shot.index}: seed image not found: {source}")
            progress(f"    shot {shot.index}: seeding from {source.name}")
            try:
                seed_url = await generator.upload_image(source)
            except Exception as exc:  # noqa: BLE001 — upload/key/quota all land here
                await generator.close()
                raise ReelError(f"shot {shot.index}: could not upload the seed image — {exc}") from exc
        kind = "animating" if seed_url else "generating"
        progress(f"    shot {shot.index}: {kind} ({shot.seconds:.1f}s) — {shot.prompt[:56]}")
        try:
            produced = await generator.generate(
                style(shot.prompt),
                image_url=seed_url,
                provider=provider,
                extra_input=inputs,
            )
        except Exception as exc:  # noqa: BLE001 — provider/key/quota all land here
            # Only suggest adding a key when a key is actually what is missing. A 402
            # means the key is fine and the ACCOUNT needs credit — and the provider
            # already says exactly where to go, so repeating "add a key" there sends
            # the user to fix the one thing that is not broken.
            detail = str(exc)
            hint = (
                f" Add a key with `navig vault set {provider.value} <token>`."
                if ("not configured" in detail or "(401)" in detail or "(403)" in detail)
                else ""
            )
            raise ReelError(
                f"shot {shot.index}: {provider.value} generation failed — {detail}{hint}"
            ) from exc
        finally:
            await generator.close()
        if not produced or not produced.local_path:
            raise ReelError(f"shot {shot.index}: the provider returned no video")
        framed = dst.with_name(f"{dst.stem}-raw{dst.suffix}")
        to_vertical(Path(produced.local_path), framed, width=width, height=height, fps=fps)
        fit_duration(framed, dst, secs=shot.seconds, fps=fps)
        return dst

    if shot.image:
        source = (base_dir / shot.image) if not Path(shot.image).is_absolute() else Path(shot.image)
        if not source.exists():
            raise ReelError(f"shot {shot.index}: image not found: {source}")
        if source.suffix.lower() in FOOTAGE_SUFFIXES:
            # An `image=` pointing at a clip is FOOTAGE, reframed and cut to length rather
            # than held as a still. This is what makes a re-grade free: generated shots are
            # the only expensive thing in the pipeline, and without it, changing a look or
            # a cut meant paying the video model again for footage already on disk.
            progress(f"    shot {shot.index}: footage {source.name} ({shot.seconds:.1f}s)")
            framed = dst.with_name(f"{dst.stem}-framed{dst.suffix}")
            to_vertical(source, framed, width=width, height=height, fps=fps)
            fit_duration(framed, dst, secs=shot.seconds, fps=fps)
            return dst
        progress(f"    shot {shot.index}: still {source.name} ({shot.seconds:.1f}s)")
        still(source, dst, secs=shot.seconds, width=width, height=height,
              fps=fps, motion=shot.motion)
        return dst

    from navig.browser import cdp_actions

    progress(f"    shot {shot.index}: capturing {shot.url} ({shot.seconds:.1f}s)")
    await cdp_actions.navigate(port, shot.url or "")
    result = await cdp_actions.record(
        port, out=str(dst), secs=shot.seconds, width=width, height=height, fps=fps,
    )
    if not result.get("ok"):
        raise ReelError(f"shot {shot.index}: capture failed — {result.get('error')}")
    return dst


async def build_lang(
    scenario_path: Path,
    out_dir: Path,
    *,
    lang: str,
    port: int,
    width: int = 1080,
    height: int = 1920,
    fps: int = 30,
    music: Path | None = None,
    duck_db: float = -12.0,
    captions: bool = True,
    progress: Progress = lambda _m: None,
) -> ReelResult:
    """Build one language's reel from ``scenario_path``."""
    from navig_audio.podcast import render as podcast_render
    from navig_audio.podcast import scenario as podcast_scenario

    from navig.media.video_edit import apply_filter, burn_captions, join, mix
    from navig_pipeline.looks import resolve as resolve_look

    episode = podcast_scenario.load(scenario_path)
    result = ReelResult(lang=lang)

    progress(f"  narrating {scenario_path.name} …")
    rendered = await podcast_render.render_episode(episode, out_dir, progress=progress)
    result.credits_spent = rendered.credits_spent

    work = out_dir / lang / "picture"
    work.mkdir(parents=True, exist_ok=True)

    # One picture segment per track, each exactly as long as that track's narration —
    # PLUS the silence the master stitches between tracks. render_episode joins the
    # tracks with TRACK_GAP_S of silence, so picture built from the track durations
    # alone comes out short by one gap per seam and the whole reel drifts early.
    gap = getattr(podcast_render, "TRACK_GAP_S", 0.0)
    track_pictures: list[Path] = []
    last_index = len(rendered.tracks) - 1
    for index, (track, done) in enumerate(zip(episode.tracks, rendered.tracks)):
        on_screen = done.duration_s + (gap if index < last_index else 0.0)
        planned, note = plan_shots(track.shots, on_screen, track=f"{track.number:02d}")
        if note:
            result.notes.append(note)
            progress(f"  ⚠ {note}")
        result.shots += len(planned)
        clips: list[Path] = []
        for shot in planned:
            dst = work / f"t{track.number:02d}_s{shot.index:02d}.mp4"
            clips.append(await _capture(
                shot, dst, port=port, width=width, height=height, fps=fps,
                base_dir=scenario_path.parent, progress=progress,
                style=episode.styled_prompt,
            ))
            result.captured += 1
        picture = work / f"t{track.number:02d}.mp4"
        join(clips, picture, fps=fps)
        track_pictures.append(picture)

    progress("  assembling …")
    silent = work / "picture.mp4"
    join(track_pictures, silent, fps=fps)

    # The look is applied to the ASSEMBLED picture, not per shot: an effect keyed to a
    # cut needs to know where the cuts are, and that is only true once the shots are in
    # one timeline. Cuts fall on the track boundaries — the joins the viewer actually
    # sees — so a glitch lands on a change of subject rather than at an arbitrary second.
    look = resolve_look(episode.meta.get("look"), episode.meta.get("look_overrides"))
    if look is not None:
        cuts, elapsed = [], 0.0
        for done in rendered.tracks[:-1]:
            elapsed += done.duration_s + gap
            cuts.append(elapsed)
        vf = look.picture_filter(beats=cuts, cuts=cuts, fps=fps)
        if vf:
            progress(f"  look: {look.name}")
            styled = work / "styled.mp4"
            apply_filter(silent, styled, vf, fps=fps)
            silent = styled

    if captions and rendered.master_subtitles and rendered.master_subtitles.exists():
        captioned = work / "captioned.mp4"
        burn_captions(silent, rendered.master_subtitles, captioned)
        silent = captioned

    final = out_dir / lang / f"reel-{episode.slug}.{lang}.mp4"
    scored = mix(
        silent, final,
        voice=rendered.master if rendered.master and rendered.master.exists() else None,
        bed=music if music and music.exists() else None,
        duck_db=duck_db,
    )
    result.path = scored.path
    result.duration_s = scored.duration_s
    return result
