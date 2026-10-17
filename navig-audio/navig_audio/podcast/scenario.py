"""Scenario parsing — a Markdown episode script becomes a structured episode.

The format is deliberately strict, and the strictness is the point. A scenario file as
people actually write them mixes three kinds of content: prose that should be spoken,
production notes that must never be spoken, and cues for music and effects. Guessing
between them is the one failure this module exists to prevent — a stage direction read
aloud in the middle of an episode is worse than a parse error, because nothing catches
it until you listen to the finished file.

So the rules are mechanical:

* ``---`` YAML frontmatter carries the episode metadata and the speaker to voice map.
* ``##`` headings cut the episode into **tracks**. An episode is an album, not one
  file: each track renders, caches and re-renders on its own.
* ``[music: ...]`` and ``[sfx: ...]`` are cues, kept in reading order with the speech
  around them so the track assembles in the order it was written.
* ``[shot: ...]`` is **picture**, not sound, in one of three forms: ``url=`` captures the
  running app, ``prompt=`` generates the footage with an AI video model, and ``image=``
  holds a still. Shots are collected onto the track instead of into its segments, so an
  audio-only render of a scenario carrying them is byte-identical to one without — they
  cost nothing and are never spoken.
* Block quotes, HTML comments and list bullets are **notes**. Never spoken, never billed.
* Anything else is speech. A block starting ``SPEAKER:`` belongs to that speaker; a
  bare block belongs to ``default_speaker``.

Blocks are separated by blank lines, so a wrapped paragraph stays one utterance rather
than being chopped into a line-per-sentence performance.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# A speaker tag has to be unmistakable, because the alternative is misreading ordinary
# prose as a speaker change. Requiring ALL CAPS means "Note: this is fine" stays speech
# while "NOBI: hello" switches voice.
_SPEAKER_RE = re.compile(r"^([A-Z][A-Z0-9_-]{1,23}):\s*(.*)$")
_CUE_RE = re.compile(r"^\[(music|sfx)\s*:\s*(.+?)\]\s*$", re.IGNORECASE)
_SHOT_RE = re.compile(r"^\[shot\s*:\s*(.+?)\]\s*$", re.IGNORECASE)
# key=value, value optionally quoted — a URL with an `&` or a label with spaces both
# have to survive, so bare values stop at whitespace and quoted ones do not.
_SHOT_ARG_RE = re.compile(r"""(\w+)\s*=\s*(?:"([^"]*)"|'([^']*)'|(\S+))""")
_HEADING_RE = re.compile(r"^##\s+(.*\S)\s*$")
_TRACK_NUM_RE = re.compile(r"^(\d{1,3})\s*[.)—–:-]?\s*(.*)$")
_BULLET_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
# "8s", "8 sec", "8 seconds" — a duration hint inside a cue prompt.
_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:s\b|sec\b|secs\b|seconds\b)", re.IGNORECASE)

DEFAULT_MODEL = "eleven_multilingual_v2"
DEFAULT_SPEAKER = "HOST"


class ScenarioError(ValueError):
    """The scenario file cannot be read as an episode. Always actionable."""


@dataclass(frozen=True)
class Cue:
    """A music or sound-effect instruction, rendered by the generation endpoints."""

    kind: str  # "music" | "sfx"
    prompt: str
    duration_s: float | None = None

    @property
    def is_speech(self) -> bool:
        return False


@dataclass(frozen=True)
class Shot:
    """A picture instruction — what is on screen while this track is spoken.

    Deliberately NOT a :class:`Cue`. A cue is generated as audio and appears in the
    track's segment stream; a shot is neither spoken nor billed, so it is carried
    alongside the segments rather than inside them. That separation is what lets the
    same scenario render as a podcast today and a video tomorrow.
    """

    url: str | None = None
    image: str | None = None
    prompt: str | None = None
    provider: str | None = None
    secs: float | None = None
    motion: str = "none"
    raw: str = ""

    @property
    def source(self) -> str:
        """Which of the three kinds of shot this is: captured, generated, or a still."""
        if self.url:
            return "capture"
        if self.prompt:
            return "generate"
        return "still"

    @property
    def is_speech(self) -> bool:
        return False


@dataclass(frozen=True)
class Line:
    """One utterance by one speaker."""

    speaker: str
    text: str

    @property
    def is_speech(self) -> bool:
        return True


@dataclass(frozen=True)
class VoiceSpec:
    """How a speaker should sound. ``voice_id`` may be absent while still planning."""

    voice_id: str | None = None
    settings: dict[str, Any] = field(default_factory=dict)


@dataclass
class Track:
    """One album track — a titled section of the episode."""

    number: int
    title: str
    slug: str
    segments: list[Line | Cue] = field(default_factory=list)
    # Picture, kept out of `segments` on purpose — see Shot.
    shots: list[Shot] = field(default_factory=list)

    @property
    def lines(self) -> list[Line]:
        return [s for s in self.segments if isinstance(s, Line)]

    @property
    def cues(self) -> list[Cue]:
        return [s for s in self.segments if isinstance(s, Cue)]

    @property
    def text(self) -> str:
        """Everything spoken in this track, as one string."""
        return "\n\n".join(line.text for line in self.lines)

    @property
    def billable_chars(self) -> int:
        """Characters that will actually be charged — speech only, cues excluded."""
        return sum(len(line.text) for line in self.lines)

    @property
    def speakers(self) -> list[str]:
        seen: list[str] = []
        for line in self.lines:
            if line.speaker not in seen:
                seen.append(line.speaker)
        return seen

    def stem(self) -> str:
        return f"{self.number:02d}-{self.slug}"


@dataclass
class Episode:
    """A parsed scenario, ready to cost, translate or render."""

    title: str
    slug: str
    lang: str
    tracks: list[Track]
    number: int | None = None
    model: str = DEFAULT_MODEL
    default_speaker: str = DEFAULT_SPEAKER
    voices: dict[str, VoiceSpec] = field(default_factory=dict)
    source: Path | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    # House art direction applied to every generated shot. A project's style spec is
    # long and load-bearing; repeating it per shot guarantees the shots drift apart.
    video_style: str | None = None

    def styled_prompt(self, scene: str) -> str:
        """A shot's scene wrapped in the episode's house style.

        ``{SCENE}`` is substituted where the style names it — which is how a style spec
        written as a template ("replace {SCENE}, keep the rest verbatim") drops straight
        in. A style without the placeholder is appended instead, so a one-line style
        still works.
        """
        if not self.video_style:
            return scene
        if "{SCENE}" in self.video_style:
            return self.video_style.replace("{SCENE}", scene)
        return f"{scene}\n\n{self.video_style}"

    @property
    def billable_chars(self) -> int:
        return sum(t.billable_chars for t in self.tracks)

    @property
    def dirname(self) -> str:
        """Folder name for this episode's rendered output."""
        prefix = f"ep{self.number:03d}-" if self.number is not None else ""
        return f"{prefix}{self.slug}"

    def track(self, number: int) -> Track:
        for t in self.tracks:
            if t.number == number:
                return t
        available = ", ".join(str(t.number) for t in self.tracks) or "none"
        raise ScenarioError(f"no track {number} in this episode (have: {available})")

    def voice_for(self, speaker: str) -> VoiceSpec:
        """The voice for ``speaker``, or a clear error naming what to add.

        Falling back to some default voice would be the wrong kindness: a whole episode
        rendered in the wrong voice sounds finished, so the mistake survives review.
        """
        spec = self.voices.get(speaker)
        if spec is None:
            known = ", ".join(sorted(self.voices)) or "none"
            raise ScenarioError(
                f"speaker {speaker!r} has no entry under `voices:` in the frontmatter "
                f"(defined: {known})"
            )
        return spec

    def unvoiced_speakers(self) -> list[str]:
        """Speakers with no usable ``voice_id`` — planning works, rendering will not."""
        missing: list[str] = []
        for track in self.tracks:
            for speaker in track.speakers:
                spec = self.voices.get(speaker)
                if (spec is None or not spec.voice_id) and speaker not in missing:
                    missing.append(speaker)
        return missing


def slugify(text: str, *, fallback: str = "track") -> str:
    """ASCII slug, accents folded — "L'Episode Zero" becomes ``l-episode-zero``.

    Track slugs become filenames, and these scripts are French, so folding accents is
    not cosmetic: it keeps output portable across filesystems and archive tools.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_text = "".join(c for c in decomposed if not unicodedata.combining(c))
    ascii_text = ascii_text.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
    return slug or fallback


def _split_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    """Peel the YAML frontmatter off the top of the document."""
    text = raw.lstrip("﻿")
    if not text.startswith("---"):
        return {}, text
    parts = text.split("\n", 1)
    if len(parts) < 2:
        return {}, text
    closing = re.search(r"^---\s*$", parts[1], re.MULTILINE)
    if not closing:
        raise ScenarioError(
            "the frontmatter block opens with `---` but never closes — "
            "add a closing `---` line after the metadata"
        )
    head, body = parts[1][: closing.start()], parts[1][closing.end():]
    try:
        loaded = yaml.safe_load(head) or {}
    except yaml.YAMLError as exc:
        raise ScenarioError(f"the frontmatter is not valid YAML: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ScenarioError("the frontmatter must be a mapping of `key: value` pairs")
    return loaded, body.lstrip("\n")


def _parse_voices(raw: Any) -> dict[str, VoiceSpec]:
    """Read the ``voices:`` map, tolerating both shorthand and full forms."""
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise ScenarioError("`voices:` must be a mapping of SPEAKER to a voice id or settings")
    voices: dict[str, VoiceSpec] = {}
    for speaker, value in raw.items():
        name = str(speaker).strip()
        if isinstance(value, str):
            # Shorthand: `NOBI: <voice_id>` for the common single-voice case.
            voices[name] = VoiceSpec(voice_id=value.strip() or None)
            continue
        if not isinstance(value, dict):
            raise ScenarioError(
                f"voice for {name!r} must be a voice id string or a mapping with `voice_id`"
            )
        settings = {
            k: v for k, v in value.items()
            if k in {"stability", "similarity_boost", "style", "speed", "use_speaker_boost"}
            and v is not None
        }
        vid = value.get("voice_id")
        voices[name] = VoiceSpec(voice_id=str(vid).strip() if vid else None, settings=settings)
    return voices


def _strip_notes(block: str) -> str:
    """Drop the parts of a block that are notes rather than speech."""
    kept: list[str] = []
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(">") or _BULLET_RE.match(line):
            continue
        kept.append(stripped)
    return " ".join(kept).strip()


def _parse_cue(line: str) -> Cue | None:
    match = _CUE_RE.match(line.strip())
    if not match:
        return None
    kind = match.group(1).lower()
    prompt = match.group(2).strip()
    duration = _DURATION_RE.search(prompt)
    return Cue(
        kind=kind,
        prompt=prompt,
        duration_s=float(duration.group(1)) if duration else None,
    )


def _parse_shot(line: str) -> Shot | None:
    """Parse ``[shot: url="…" secs=3.4 motion="drift"]``, or return None if not a shot."""
    match = _SHOT_RE.match(line.strip())
    if not match:
        return None
    body = match.group(1).strip()
    args: dict[str, str] = {}
    for key, double, single, bare in _SHOT_ARG_RE.findall(body):
        args[key.lower()] = double or single or bare
    url = args.get("url") or None
    image = args.get("image") or args.get("file") or None
    prompt = args.get("prompt") or None
    if not url and not image and not prompt:
        # A shot naming no source would render as a silent black gap with nothing to
        # explain it. Refusing here costs one clear error; allowing it costs a reshoot.
        raise ScenarioError(
            f"[shot: {body}] names no source — give it `url=` (capture the running app), "
            "`prompt=` (generate the footage) or `image=` (a still)"
        )
    # Capturing AND generating the same shot is not a blend, it is two answers to one
    # question — and silently preferring one would waste whichever the author meant.
    if url and prompt:
        raise ScenarioError(
            f"[shot: {body}] has both `url=` and `prompt=` — a shot is either captured "
            "from the running app or generated, not both"
        )
    raw_secs = args.get("secs") or args.get("duration")
    secs: float | None = None
    if raw_secs:
        try:
            secs = float(raw_secs)
        except ValueError as exc:
            raise ScenarioError(f"[shot: {body}] has a non-numeric secs={raw_secs!r}") from exc
        if secs <= 0:
            raise ScenarioError(f"[shot: {body}] has secs={raw_secs} — it must be positive")
    return Shot(
        url=url, image=image, prompt=prompt,
        provider=(args.get("provider") or None), secs=secs,
        motion=(args.get("motion") or "none").lower(), raw=body,
    )


def _parse_block(block: str, default_speaker: str) -> list[Line | Cue]:
    """Turn one blank-line-separated block into ordered segments.

    A block may hold several utterances: a ``SPEAKER:`` tag closes whatever came before
    it and opens a new one, so a back-and-forth written without blank lines between the
    turns still renders as separate voices instead of one speaker reading the other's
    name aloud.
    """
    segments: list[Line | Cue] = []
    speech_lines: list[str] = []
    speaker = default_speaker

    def flush() -> None:
        text = _strip_notes("\n".join(speech_lines))
        if text:
            segments.append(Line(speaker=speaker, text=text))
        speech_lines.clear()

    for raw_line in block.splitlines():
        if not raw_line.strip():
            continue
        cue = _parse_cue(raw_line)
        if cue is not None:
            # A cue interrupts the speech around it, so close the current utterance
            # first — otherwise the track would assemble out of order.
            flush()
            segments.append(cue)
            continue
        match = _SPEAKER_RE.match(raw_line.strip())
        if match:
            flush()
            speaker = match.group(1)
            remainder = match.group(2).strip()
            if remainder:
                speech_lines.append(remainder)
            continue
        speech_lines.append(raw_line)

    flush()
    return segments


def _parse_heading(heading: str, position: int) -> tuple[int, str]:
    """Split "01 - Intro" into its number and title, numbering by position if absent."""
    match = _TRACK_NUM_RE.match(heading)
    if match:
        title = match.group(2).strip()
        number = int(match.group(1))
        return number, title or f"Track {number}"
    return position, heading


def parse(raw: str, *, source: Path | None = None) -> Episode:
    """Parse scenario text into an :class:`Episode`."""
    meta, body = _split_frontmatter(raw)
    body = _COMMENT_RE.sub("", body)

    default_speaker = str(meta.get("default_speaker") or DEFAULT_SPEAKER).strip()
    voices = _parse_voices(meta.get("voices"))

    tracks: list[Track] = []
    state: dict[str, Any] = {"heading": None, "buffer": []}

    def close_track() -> None:
        heading = state["heading"]
        if heading is None:
            return
        number, title = _parse_heading(heading, len(tracks) + 1)
        # Lift shots out BEFORE the speech is blocked up, so the segment stream a
        # scenario with shots produces is identical to one without them.
        shots: list[Shot] = []
        spoken: list[str] = []
        for line in state["buffer"]:
            shot = _parse_shot(line)
            if shot is not None:
                shots.append(shot)
            else:
                spoken.append(line)
        segments: list[Line | Cue] = []
        for block in re.split(r"\n\s*\n", "\n".join(spoken)):
            if block.strip():
                segments.extend(_parse_block(block, default_speaker))
        tracks.append(
            Track(
                number=number, title=title, slug=slugify(title),
                segments=segments, shots=shots,
            )
        )

    for line in body.splitlines():
        heading = _HEADING_RE.match(line)
        if heading:
            close_track()
            state["heading"] = heading.group(1).strip()
            state["buffer"] = []
            continue
        if state["heading"] is not None:
            state["buffer"].append(line)
    close_track()

    if not tracks:
        raise ScenarioError(
            "no tracks found — an episode needs at least one `## Track title` heading. "
            "If this is a rough outline, convert it first with `navig audio draft`."
        )

    # Duplicate numbers would collide on disk and silently overwrite a rendered track.
    seen: dict[int, str] = {}
    for track in tracks:
        if track.number in seen:
            raise ScenarioError(
                f"two tracks are numbered {track.number} "
                f"({seen[track.number]!r} and {track.title!r}) — renumber the headings"
            )
        seen[track.number] = track.title

    title = str(meta.get("title") or (source.stem if source else "Untitled episode"))
    number = meta.get("episode")
    return Episode(
        title=title,
        slug=str(meta.get("slug") or slugify(title, fallback="episode")),
        lang=str(meta.get("lang") or "en").lower(),
        tracks=tracks,
        number=int(number) if number is not None else None,
        model=str(meta.get("model") or DEFAULT_MODEL),
        default_speaker=default_speaker,
        voices=voices,
        source=source,
        meta=meta,
        video_style=(str(meta["video_style"]) if meta.get("video_style") else None),
    )


def load(path: Path | str) -> Episode:
    """Parse a scenario file, reporting the path in any error."""
    path = Path(path)
    if not path.exists():
        raise ScenarioError(f"scenario not found: {path}")
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ScenarioError(f"{path.name} is not UTF-8 text — re-save it as UTF-8") from exc
    try:
        return parse(raw, source=path)
    except ScenarioError as exc:
        raise ScenarioError(f"{path.name}: {exc}") from exc


def dump(episode: Episode) -> str:
    """Render an :class:`Episode` back to strict scenario Markdown.

    Translation round-trips through here, so the emitted form has to be exactly what
    :func:`parse` accepts — otherwise a translated file would fail to render.
    """
    front: dict[str, Any] = {}
    if episode.number is not None:
        front["episode"] = episode.number
    front["slug"] = episode.slug
    front["title"] = episode.title
    front["lang"] = episode.lang
    front["model"] = episode.model
    front["default_speaker"] = episode.default_speaker
    if episode.voices:
        front["voices"] = {
            name: ({"voice_id": spec.voice_id, **spec.settings} if spec.settings
                   else (spec.voice_id or ""))
            for name, spec in episode.voices.items()
        }

    head = yaml.safe_dump(front, allow_unicode=True, sort_keys=False).rstrip()
    out = [f"---\n{head}\n---\n"]
    for track in episode.tracks:
        out.append(f"## {track.number:02d} — {track.title}\n")
        for segment in track.segments:
            if isinstance(segment, Cue):
                out.append(f"[{segment.kind}: {segment.prompt}]\n")
            else:
                out.append(f"{segment.speaker}: {segment.text}\n")
        out.append("")
    return "\n".join(out).rstrip() + "\n"
