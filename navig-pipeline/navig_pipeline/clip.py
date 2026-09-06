"""Clips — picture cut to a track that already exists.

:mod:`navig_pipeline.reel` is narration-first: it renders the voice, learns how long each
line turned out to be, and fits the picture to that. A song has no narration to fit to, and
asking the reel builder for one is why it refuses — ``plan_shots`` raises rather than cut
picture against a length nobody measured.

This is the other half. **The audio is a given.** Its duration is measured once with
ffprobe, the sections divide it, and every shot is sized so the picture ends exactly with
the last sample. Nothing here narrates, nothing here bills a TTS provider, and nothing here
opens a browser unless a shot actually asks to film one.

The shotlist is the *same* Markdown scenario format ``navig audio`` and ``navig pipeline
reel`` already use — deliberately, so a song's picture needs no second syntax. A shotlist is
simply a scenario with no speech in it: the parser needs one heading and nothing else, so a
section holding only shot lines is valid.

**Where a cut lands.** navig has no beat detection — :func:`navig.media.fx.punch_in` takes
beat times, it does not find them. So a section may be pinned to an absolute second in the
frontmatter::

    sections: [0, 3.2, 6.4, 9.4]

and those become the cuts, which is how picture lands on the music without pretending to
analyse it. Without ``sections:`` the audio is divided in proportion to each section's
written ``secs=``, which is the same relative-pacing rule ``reel`` uses.
"""

from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from navig_pipeline.reel import MIN_SHOT_S, PlannedShot, _capture, plan_shots

_log = logging.getLogger(__name__)

Progress = Callable[[str], None]

# A pinned section boundary may disagree with the audio by this much before we treat it as a
# mistake rather than rounding. Half a frame at 30fps is ~0.017s; a tenth of a second is
# comfortably below anything a listener hears as early.
SECTION_EPSILON_S = 0.1


class ClipError(RuntimeError):
    """The clip could not be built — bad shotlist, missing audio, or a failed shot."""


def _installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):  # pragma: no cover - find_spec on a broken pkg
        return False


def needs_capture(episode: Any) -> bool:
    """True when any shot films a live URL — the only reason a clip needs a browser."""
    return any(
        getattr(shot, "url", None)
        for track in getattr(episode, "tracks", [])
        for shot in getattr(track, "shots", [])
    )


def detect_capabilities(episode: Any = None) -> dict[str, bool]:
    """Which parts of the clip stage can run right now.

    ``capture`` is reported ONLY when the shotlist actually films something. ``reel`` asks
    for a browser unconditionally and starts one even for an all-generated scenario; here a
    song made of generated shots and stills must not open a browser it never uses, because a
    browser nobody closes is a browser nobody notices leaking.
    """
    caps: dict[str, bool] = {"assemble": _installed("navig.media.video_edit")}
    if caps["assemble"]:
        try:
            from navig.media.video_edit import ffmpeg_available

            caps["assemble"] = ffmpeg_available()
        except Exception:  # noqa: BLE001 — a broken ffmpeg probe is a missing capability
            caps["assemble"] = False
    if episode is not None and needs_capture(episode):
        caps["capture"] = _installed("navig.browser.cdp_actions")
    return caps


# -- the audio ----------------------------------------------------------------


def resolve_audio(shotlist: Path, episode: Any, override: Path | None = None) -> Path:
    """Find the track this shotlist is cut against.

    ``--audio`` wins; otherwise the frontmatter's ``audio:``, resolved **relative to the
    shotlist** so a shotlist and its song can move together without editing paths.
    """
    if override is not None:
        audio = Path(override)
    else:
        declared = getattr(episode, "meta", {}).get("audio")
        if not declared:
            raise ClipError(
                "no audio — add `audio: <path>` to the frontmatter or pass --audio. A clip "
                "is picture cut to a track; without the track there is no length to cut to."
            )
        audio = Path(str(declared))
        if not audio.is_absolute():
            audio = (Path(shotlist).parent / audio).resolve()
    if not audio.exists():
        raise ClipError(f"audio not found: {audio}")
    return audio


def audio_duration(audio: Path) -> float:
    """Length of the track in seconds, measured — never assumed."""
    from navig.media.audio_edit import probe_duration

    secs = float(probe_duration(audio))
    if secs <= 0:
        raise ClipError(f"{Path(audio).name} reports no duration — is it a valid audio file?")
    return secs


# -- the plan -----------------------------------------------------------------


@dataclass
class PlannedSection:
    """One titled stretch of the song, and the shots that cover it."""

    number: int
    title: str
    start: float
    seconds: float
    shots: list[PlannedShot] = field(default_factory=list)
    pinned: bool = False

    @property
    def end(self) -> float:
        return self.start + self.seconds

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "title": self.title,
            "start": round(self.start, 3),
            "seconds": round(self.seconds, 3),
            "pinned": self.pinned,
            "shots": [shot.to_dict() for shot in self.shots],
        }


@dataclass
class ClipResult:
    """What a finished (or planned) clip amounts to."""

    path: Path | None = None
    duration_s: float = 0.0
    sections: int = 0
    shots: int = 0
    generated: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path) if self.path else None,
            "duration_s": round(self.duration_s, 3),
            "sections": self.sections,
            "shots": self.shots,
            "generated": self.generated,
            "notes": self.notes,
        }


def _section_weights(tracks: list[Any]) -> list[float]:
    """How much of the song each section takes when it is not pinned.

    A section's weight is what its author wrote — the sum of its shots' ``secs=``. A section
    whose shots carry no ``secs=`` falls back to its shot count, so "more shots" still reads
    as "more time" rather than collapsing into an equal split.
    """
    weights: list[float] = []
    for track in tracks:
        shots = getattr(track, "shots", [])
        written = sum(float(getattr(s, "secs", None) or 0.0) for s in shots)
        weights.append(written if written > 0 else float(len(shots) or 1))
    return weights


def _pinned_starts(raw: Any, count: int, audio_s: float) -> list[float]:
    """Validate a frontmatter ``sections:`` list into absolute start times.

    Every failure here is a mistake that would otherwise surface as picture drifting away
    from the music halfway through — discovered on playback, not on render. So it is a hard
    error carrying the actual numbers, never a silent repair.
    """
    if not isinstance(raw, (list, tuple)):
        raise ClipError(
            f"`sections:` must be a list of start times in seconds, got {type(raw).__name__}"
        )
    try:
        starts = [float(value) for value in raw]
    except (TypeError, ValueError) as exc:
        raise ClipError(f"`sections:` must be numbers (seconds), got {list(raw)!r}") from exc
    if len(starts) != count:
        raise ClipError(
            f"`sections:` has {len(starts)} start time(s) but the shotlist has {count} "
            f"section heading(s) — they must line up one to one"
        )
    if starts[0] > SECTION_EPSILON_S:
        raise ClipError(
            f"`sections:` must start at 0, got {starts[0]:g} — picture cannot begin late"
        )
    for i in range(1, len(starts)):
        if starts[i] <= starts[i - 1]:
            raise ClipError(
                f"`sections:` must ascend — entry {i + 1} ({starts[i]:g}s) does not come "
                f"after entry {i} ({starts[i - 1]:g}s)"
            )
    if starts[-1] >= audio_s - SECTION_EPSILON_S:
        raise ClipError(
            f"`sections:` last start is {starts[-1]:g}s but the track is only {audio_s:.2f}s "
            f"— that section would have no time on screen"
        )
    starts[0] = 0.0
    return starts


def plan_sections(episode: Any, audio_s: float, grid: Any = None) -> tuple[list[PlannedSection], list[str]]:
    """Divide ``audio_s`` across the shotlist's sections, then fit each section's shots.

    Returns the plan plus any notes worth telling the user — pacing that had to be stretched,
    and content this stage cannot honour.
    """
    tracks = list(getattr(episode, "tracks", []))
    if not tracks:
        raise ClipError("the shotlist has no section headings")

    empty = [t for t in tracks if not getattr(t, "shots", [])]
    if empty:
        names = ", ".join(f"{t.number:02d} {t.title!r}" for t in empty)
        raise ClipError(
            f"section(s) {names} have no shots — the picture would go black while the music "
            f"plays. Give every section at least one shot."
        )

    notes: list[str] = []
    spoken = [t for t in tracks if getattr(t, "lines", [])]
    if spoken:
        notes.append(
            f"{len(spoken)} section(s) contain speech — a clip does not narrate, so those "
            f"lines stay picture notes and are never spoken or billed"
        )
    cued = [t for t in tracks if getattr(t, "cues", [])]
    if cued:
        notes.append(
            f"{len(cued)} section(s) contain music/sfx cues — a clip is cut against audio you "
            f"supply, so the cues are ignored and nothing is generated for them"
        )

    raw_sections = getattr(episode, "meta", {}).get("sections")
    if raw_sections is not None:
        starts = _pinned_starts(raw_sections, len(tracks), audio_s)
        pinned = True
    else:
        weights = _section_weights(tracks)
        total = sum(weights) or float(len(tracks))
        starts, elapsed = [], 0.0
        for weight in weights:
            starts.append(elapsed)
            elapsed += weight / total * audio_s
        pinned = False

    # Land the cuts ON the music. Every effect in fx has taken beat times since it was
    # written and nothing ever supplied any — callers passed the cut boundaries and called
    # them beats, which puts the hits on the edit instead of the song. Downbeats rather
    # than beats: a section change belongs at the top of a bar, not on the third sixteenth.
    if grid is not None and (getattr(episode, "meta", {}) or {}).get("beat_sync") is not False:
        targets = list(getattr(grid, "downbeats", None) or getattr(grid, "beats", []))
        if targets:
            moved = snap(starts, targets, total=audio_s)
            drift = max((abs(a - b) for a, b in zip(starts, moved)), default=0.0)
            if drift > 0.01:
                notes.append(
                    f"cuts snapped to the beat at {getattr(grid, 'bpm', 0):.0f}bpm "
                    f"(moved up to {drift:.2f}s)"
                )
            starts = moved

    bounds = [*starts[1:], audio_s]
    planned: list[PlannedSection] = []
    for index, track in enumerate(tracks):
        seconds = bounds[index] - starts[index]
        if seconds < MIN_SHOT_S:
            raise ClipError(
                f"section {track.number:02d} {track.title!r} gets only {seconds:.2f}s — below "
                f"the {MIN_SHOT_S}s floor, that is a flash frame nobody can read"
            )
        shots, note = plan_shots(
            track.shots, seconds, track=f"{track.number:02d}",
            against="audio", follows="the track", measured="of track",
        )
        # A uniform stretch is not news when the sections are unpinned: every section is
        # scaled by the SAME factor, so the warning fires once per section and says only
        # "your relative numbers were relative". Pinned sections are different — there the
        # author asserted an absolute boundary, so a stretch inside one is worth saying.
        if note and pinned:
            notes.append(note)
        if grid is not None and len(shots) > 1:
            shots = _snap_shots(shots, starts[index], seconds, grid)
        planned.append(
            PlannedSection(
                number=track.number, title=track.title, start=starts[index],
                seconds=seconds, shots=shots, pinned=pinned,
            )
        )
    return planned, notes


def _snap_shots(shots: list[PlannedShot], start: float, seconds: float,
                grid: Any) -> list[PlannedShot]:
    """Move the cuts BETWEEN a section's shots onto beats, keeping the section length.

    A section already lands on a downbeat; this is what makes the shots inside it land too.
    The section's own start and end are fixed, so only the internal boundaries move and the
    total is unchanged — picture still ends exactly with the last sample.
    """
    beats = [b - start for b in getattr(grid, "beats", []) if start < b < start + seconds]
    if not beats:
        return shots
    offsets, running = [0.0], 0.0
    for shot in shots[:-1]:
        running += shot.seconds
        offsets.append(running)
    moved = snap(offsets, beats, total=seconds)
    edges = [*moved, seconds]
    return [
        PlannedShot(
            index=shot.index, seconds=edges[i + 1] - edges[i], url=shot.url,
            image=shot.image, prompt=shot.prompt, provider=shot.provider,
            motion=shot.motion,
        )
        for i, shot in enumerate(shots)
    ]


def cuts_of(sections: list[PlannedSection]) -> list[float]:
    """Where the viewer sees a change of subject — the section seams, not every shot.

    An effect keyed to a cut wants the seams that read as a cut. Firing a glitch on every
    shot boundary in a fast song is continuous noise, which reads as a broken export.
    """
    return [section.start for section in sections[1:]]


def generated_count(sections: list[PlannedSection]) -> int:
    """How many shots a video model will bill — what ``--dry-run`` has to report."""
    return sum(1 for section in sections for shot in section.shots if shot.prompt)


def resolve_video_settings(
    episode: Any, model: str | None = None
) -> tuple[str | None, dict[str, Any] | None, str | None]:
    """The video model this shotlist wants, the inputs it declares, and its seed-image key.

    The built-in defaults are keyed to ONE Replicate model. A shotlist naming a different
    one is the only thing that knows which inputs that model accepts — and Replicate
    rejects an input a model does not declare, so guessing is a 422 rather than a harmless
    extra. ``video_input:`` was documented in a comment here for months and read by
    nothing; this is it actually being read.
    """
    meta = getattr(episode, "meta", {}) or {}
    video_input = meta.get("video_input") or None
    if video_input is not None and not isinstance(video_input, dict):
        raise ClipError(
            f"`video_input:` must be a mapping of model input to value, got "
            f"{type(video_input).__name__}"
        )
    return (
        model or (str(meta["video_model"]) if meta.get("video_model") else None),
        video_input,
        str(meta["video_image_key"]) if meta.get("video_image_key") else None,
    )

def resolve_look_for(episode: Any, override: str | None = None) -> Any:
    """The grade for this clip: a named format, or a spec the shotlist carries itself.

    ``look:`` names one of the built-in formats. ``look_spec:`` is the escape hatch for a
    project with its own art direction — the built-in registry is a closed, tested set
    belonging to one universe, and a song from a different one should not have to join it
    to get a grade. Naming both is a hard error rather than a precedence rule nobody can
    remember from the output.
    """
    from navig_pipeline.looks import from_spec
    from navig_pipeline.looks import resolve as resolve_named

    meta = getattr(episode, "meta", {})
    named = override or meta.get("look")
    spec = meta.get("look_spec")
    if named and spec:
        raise ClipError(
            "the shotlist sets both `look:` and `look_spec:` — use one. `look:` names a "
            "built-in format; `look_spec:` defines your own."
        )
    if spec:
        try:
            return from_spec(spec)
        except (KeyError, TypeError) as exc:
            raise ClipError(f"bad `look_spec:` — {exc}") from exc
    try:
        return resolve_named(named, meta.get("look_overrides"))
    except KeyError as exc:
        raise ClipError(f"bad `look:` — {exc}") from exc

# -- the build ----------------------------------------------------------------


async def build(
    shotlist: Path,
    audio: Path,
    out_dir: Path,
    *,
    look_name: str | None = None,
    width: int = 1080,
    height: int = 1920,
    fps: int = 30,
    captions: Path | None = None,
    model: str | None = None,
    bpm: float | None = None,
    port: int = 0,
    progress: Progress = lambda _m: None,
) -> ClipResult:
    """Build one vertical clip from ``shotlist``, cut to ``audio``."""
    from navig_audio.podcast import scenario as podcast_scenario

    from navig.media.video_edit import apply_filter, burn_captions, join, mix

    shotlist = Path(shotlist)
    episode = podcast_scenario.load(shotlist)
    audio_s = audio_duration(audio)
    grid = resolve_beats(episode, audio, bpm)
    sections, notes = plan_sections(episode, audio_s, grid)
    if grid is not None:
        progress(f"  beat: {grid.bpm:.1f}bpm, {len(grid.beats)} beats, "
                 f"{len(grid.downbeats)} downbeats (confidence {grid.confidence:.2f})")
        from navig.media.beats import LOW_CONFIDENCE

        if grid.confidence < LOW_CONFIDENCE:
            notes.append(
                f"tempo is a guess at {grid.bpm:.0f}bpm (confidence {grid.confidence:.2f}) — "
                f"a short cut has too few bars to be sure, and it fails at an exact "
                f"multiple. Set `bpm:` if a longer mix of the same session is known."
            )

    video_model, video_input, image_key = resolve_video_settings(episode, model)
    meta = getattr(episode, "meta", {}) or {}
    transition = str(meta.get("transition") or "cut").lower()
    effect = str(meta.get("transition_effect") or "fade")
    transition_s = float(meta.get("transition_s") or 0.25)

    result = ClipResult(sections=len(sections), notes=list(notes))
    for note in notes:
        progress(f"  ! {note}")

    work = out_dir / "picture"
    work.mkdir(parents=True, exist_ok=True)

    # ONE flat timeline. Both the transitions and the look need to know where every cut
    # is, and joining per-section first hid the shot boundaries inside a section from both.
    timeline = [(section, shot) for section in sections for shot in section.shots]
    # xfade CONSUMES time: joining n clips with a T-second crossfade gives
    # sum(d) - (n-1)*T. Left alone the picture ends early and breaks the one promise this
    # module makes, so every shot is lengthened up front to pay for the fades.
    stretch = 1.0
    if transition == "xfade" and len(timeline) > 1 and transition_s > 0:
        stretch = (audio_s + (len(timeline) - 1) * transition_s) / audio_s
        progress(f"  transition: {effect} {transition_s:g}s (shots +{(stretch - 1) * 100:.1f}% to pay for it)")

    clips: list[Path] = []
    current = -1
    for section, shot in timeline:
        if section.number != current:
            current = section.number
            progress(f"  {section.number:02d} {section.title} - {section.seconds:.2f}s")
        dst = work / f"s{section.number:02d}_{shot.index:02d}.mp4"
        clips.append(await _capture(
            replace(shot, seconds=shot.seconds * stretch) if stretch != 1.0 else shot,
            dst, port=port, width=width, height=height, fps=fps,
            base_dir=shotlist.parent, progress=progress,
            style=episode.styled_prompt, model=video_model,
            extra_input=video_input, image_key=image_key,
        ))
        result.shots += 1
        if shot.prompt:
            result.generated += 1

    progress("  assembling ...")
    silent = work / "picture.mp4"
    join(clips, silent, fps=fps, transition=transition, transition_s=transition_s,
         effect=effect)

    # The look goes on the ASSEMBLED picture, exactly as in reel: an effect keyed to a cut
    # has to know where the cuts are, and that is only true once the shots share a timeline.
    look = resolve_look_for(episode, look_name)
    if look is not None:
        # Beats drive the pulse, downbeats drive the tears. Before this, BOTH were the
        # section boundaries — so every effect fired on the edit rather than on the song,
        # which is the exact thing a viewer reads as "not cut to the music".
        if grid is not None:
            beats = thin(grid.beats)
            hits = thin(grid.downbeats or grid.beats)
        else:
            beats = hits = cuts_of(sections)
        font = str(meta["font"]) if meta.get("font") else None
        vf = look.picture_filter(beats=beats, cuts=hits, fps=fps, font=font)
        if vf:
            progress(f"  look: {look.name}")
            styled = work / "styled.mp4"
            apply_filter(silent, styled, vf, fps=fps)
            silent = styled

    if captions is not None:
        if not Path(captions).exists():
            raise ClipError(f"captions not found: {captions}")
        progress(f"  captions: {Path(captions).name}")
        captioned = work / "captioned.mp4"
        burn_captions(silent, Path(captions), captioned)
        silent = captioned

    final = out_dir / f"clip-{episode.slug}.mp4"
    # The song goes in the VOICE slot, not the bed. `mix` attenuates a bed by `duck_db` —
    # right for music under a voice, wrong for music that IS the track, which would come out
    # a silent-looking 12 dB down with nothing in the log to say why.
    scored = mix(silent, final, voice=audio)
    result.path = scored.path
    result.duration_s = scored.duration_s
    return result


# -- the beat -----------------------------------------------------------------

# How many timestamps an effect may carry before it is thinned. punch_in builds one term
# per beat inside a single zoompan expression, and a two-minute track at 92bpm is 217 of
# them — ffmpeg parses that, slowly, and the result is a frame that never sits still.
MAX_EFFECT_HITS = 96


def resolve_beats(episode: Any, audio: Path, bpm: float | None = None) -> Any:
    """Find the beat grid for this track, or None when the shotlist opts out.

    ``beats: false`` in the frontmatter turns detection off. ``bpm:`` supplies a tempo
    that is already known, which matters more than it sounds: a 12-second fragment does
    not contain enough bars for autocorrelation to be sure, and it fails by returning an
    exact multiple of the truth.
    """
    meta = getattr(episode, "meta", {}) or {}
    if meta.get("beats") is False:
        return None
    declared = bpm if bpm is not None else meta.get("bpm")
    try:
        from navig.media.beats import BeatError, detect
    except ImportError:  # pragma: no cover - numpy is a declared dependency
        return None
    try:
        return detect(audio, bpm=float(declared) if declared else None)
    except BeatError as exc:
        raise ClipError(
            f"could not find the beat in {Path(audio).name} — {exc}. "
            f"Set `beats: false` to cut without it, or `bpm: <tempo>` if you know it."
        ) from exc


def thin(times: list[float], limit: int = MAX_EFFECT_HITS) -> list[float]:
    """Keep at most ``limit`` evenly-spaced timestamps.

    Thinning by taking every n-th rather than the first n: an effect that fires for the
    opening thirty seconds and then stops looks broken, where one that fires half as often
    throughout looks deliberate.
    """
    if limit < 1:
        raise ValueError(f"limit must be at least 1, got {limit}")
    if len(times) <= limit:
        return list(times)
    step = (len(times) + limit - 1) // limit
    return times[::step]


def snap(starts: list[float], targets: list[float], *, total: float,
         min_gap: float = MIN_SHOT_S) -> list[float]:
    """Move each boundary onto the nearest beat, without reordering or crushing anything.

    The first boundary is always 0 — the picture starts when the track does. Every other
    one moves to its closest target and is then forced to stay at least ``min_gap`` after
    the previous one and ``min_gap`` before the end, so snapping can shorten a shot but
    can never invert two of them or produce a frame nobody can see.
    """
    if not targets or not starts:
        return list(starts)
    snapped = [0.0]
    for original in starts[1:]:
        nearest = min(targets, key=lambda t: abs(t - original))
        floor = snapped[-1] + min_gap
        ceiling = total - min_gap * (len(starts) - len(snapped))
        snapped.append(max(floor, min(nearest, ceiling)) if ceiling > floor else floor)
    return snapped
