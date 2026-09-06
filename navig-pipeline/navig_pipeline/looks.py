"""Looks — a format is a preset, not a rewrite.

A reel's *format* (an old broadcast, an operator's helmet feed, a found tape) is almost
entirely a fixed set of grade, texture and overlay choices. Encoding each as a named look
means a new format is a few lines of data rather than a branch through the renderer, and
it means two scenarios claiming the same format cannot quietly drift apart.

A look is selected with one key in the scenario frontmatter::

    look: broadcast

and may be adjusted per scenario without editing this file::

    look: broadcast
    look_overrides: { grain: 4, scanlines: false }

Looks compose fragments from :mod:`navig.media.fx`. They deliberately do **not** carry art
direction — that already lives in `E:\\prompts\\projects\\schema\\images\\brand-visual-styles.md`
as the SCH-1…SCH-10 presets, and a look names one rather than restating it, so the two
cannot disagree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from navig.media import fx


@dataclass(frozen=True)
class Look:
    """One named format.

    ``preset`` and ``register`` are documentation that travels with the render: they say
    which existing art-direction block a generated shot should use, so the look and the
    prompt library stay in agreement instead of both describing the palette badly.
    """

    name: str
    description: str
    preset: str | None = None          # SCH-1…SCH-10 in brand-visual-styles.md
    register: str | None = None        # "universe-art" (the joke) | "sideeffects" (the mystery)

    # texture
    grain: float = 0.0
    vhs_bleed: float = 0.0
    scanlines: bool = False
    saturation: float = 1.0
    # Hold ONE colour and grey the rest, instead of flattening everything. `None` means
    # ordinary saturation. This is what keeps a period grade from erasing the brand.
    accent: str | None = None
    accent_similarity: float = 0.30
    contrast: float = 1.0
    # -1..1, 0 = untouched. Without it a look can only darken: contrast pulls the shadows
    # down and the vignette pulls the corners down, and on already-dark art the two
    # compound with nothing to answer them.
    brightness: float = 0.0
    softness: float = 0.0
    vignette: float = 0.0
    letterbox: float = 1.0             # 1.0 = fills the frame

    # motion
    punch: float = 0.0                 # 0 disables; else the zoom amount on a beat
    shake: float = 0.0
    glitch_strength: int = 0
    # A white kick on the beat. Small numbers only — this is the effect that most reliably
    # tips a clip from "cut to the music" into "unwatchable".
    flash: float = 0.0

    overlays: list[dict[str, Any]] = field(default_factory=list)

    def picture_filter(self, *, beats: list[float] | None = None,
                       cuts: list[float] | None = None,
                       fps: int = 30, font: str | None = None) -> str:
        """The filter chain for this look, given where the beats and cuts fall.

        Order matters and is not arbitrary: motion first (it resamples the image), then
        the frame, then grade, then texture. Grading before a blur would be undone by it,
        and adding grain before a scale would resample the grain into mush — which reads
        as compression noise rather than as film.
        """
        return fx.chain(
            fx.punch_in(beats or [], amount=1 + self.punch, fps=fps) if self.punch else "",
            fx.shake(self.shake),
            fx.letterbox(self.letterbox),
            fx.hold_accent(self.accent, similarity=self.accent_similarity)
            if self.accent else "",
            fx.broadcast_grade(
                saturation=self.saturation, contrast=self.contrast,
                softness=self.softness, brightness=self.brightness,
            ),
            fx.vhs(bleed=self.vhs_bleed, scanlines=self.scanlines),
            fx.glitch(cuts or [], strength=self.glitch_strength),
            # On the CUTS, not every beat. A white kick every beat at 92bpm is a strobe
            # twice a second; on the downbeat it is a hit.
            fx.flash(cuts or [], amount=self.flash) if self.flash else "",
            fx.grain(self.grain),
            fx.vignette(self.vignette),
            # Overlays go LAST, on top of the finished grade. Anywhere earlier and the
            # colour hold greys the text and the grain eats its edges — the caption ends
            # up looking like part of the footage instead of on top of it.
            self.overlay_filter(font=font, fps=fps),
        )

    def overlay_filter(self, *, font: str | None = None, fps: int = 30) -> str:
        """The look's text overlays, or "" when there are none or no font to draw them.

        ``overlays`` was declared on this dataclass, populated by BROADCAST, and rendered
        by nothing at all — a format that claimed a PLAY badge and a timecode and shipped
        neither. This is it actually being drawn.

        A missing font skips the overlays rather than failing the render: drawtext needs a
        real font FILE (it has no name lookup), and losing a caption is not worth losing
        the video.
        """
        if not self.overlays:
            return ""
        face = font or fx.default_font()
        if not face:
            return ""
        stamped = datetime.now().strftime("%d.%m.%Y")
        parts = []
        for item in self.overlays:
            raw = str(item.get("text", "")).replace("{DATE}", stamped)
            if not raw:
                continue
            pos = str(item.get("pos", "top-left"))
            size = int(item.get("size", 28))
            colour = str(item.get("colour", "white"))
            alpha = float(item.get("alpha", 1.0))
            enable = str(item["enable"]) if item.get("enable") else None
            # {TIMECODE} is a live clock, not a string. Rendering the placeholder as text
            # would put the literal characters on screen, which is worse than omitting it.
            lines = [ln for ln in raw.splitlines() if ln.strip()]
            for line in lines:
                if "{TIMECODE}" in line:
                    parts.append(fx.timecode(
                        font=face, pos=pos, size=size, colour=colour, alpha=alpha, fps=fps,
                    ))
                else:
                    parts.append(fx.overlay(
                        line, font=face, pos=pos, size=size, colour=colour,
                        alpha=alpha, enable=enable,
                    ))
        return fx.chain(*parts)


# ── the built-in formats ──────────────────────────────────────────────────────

BROADCAST = Look(
    name="broadcast",
    description=(
        "A recovered transmission. Letterboxed inside black, scanlined, and greyed "
        "EXCEPT the ketchup accent — the period look without erasing the brand."
    ),
    preset="SCH-7",            # MISSION BRIEFING — dossier / terminal
    register="sideeffects",    # sells the mystery
    grain=6.0,
    vhs_bleed=2.0,
    scanlines=True,
    accent="0xD63A32",     # ketchup — everything else greys out
    saturation=0.85,       # gentle, because `accent` has already done the work
    contrast=1.12,
    softness=0.5,
    vignette=0.22,
    letterbox=0.86,
    punch=0.0,                 # a tape machine does not punch in
    overlays=[
        {"text": "PLAY \u25b6", "pos": "top-right", "size": 26},
        {"text": "{TIMECODE}\n{DATE}", "pos": "bottom-left", "size": 22},
    ],
)

OPERATOR = Look(
    name="operator",
    description=(
        "The Grid's enforcer. Low hero angle, hard contrast, a single coral accent, and "
        "just enough handheld to feel worn rather than mounted."
    ),
    preset="SCH-1",            # COMMAND HUD
    register="sideeffects",
    grain=4.0,
    saturation=0.7,
    contrast=1.18,
    vignette=0.45,
    punch=0.05,
    shake=2.0,
)

UNDERGROUND = Look(
    name="underground",
    description=(
        "The masked-talker meme format. Clean picture, hard digital tears on every cut, "
        "punchy on the beat. Deadpan delivery does the work; the edit supplies the energy."
    ),
    preset="SCH-1",
    register="universe-art",   # sells the joke
    grain=2.0,
    contrast=1.05,
    punch=0.09,
    glitch_strength=8,
)

FOUND_FOOTAGE = Look(
    name="found-footage",
    description=(
        "Evidence, not content. A small window inside a large black frame, degraded and "
        "slightly wrong — the format that makes a viewer lean in rather than scroll."
    ),
    preset="SCH-10",           # CIVIC BLUEPRINT — technical, documentary
    register="sideeffects",
    grain=10.0,
    vhs_bleed=3.0,
    scanlines=True,
    saturation=0.35,
    contrast=1.2,
    softness=0.8,
    vignette=0.5,
    letterbox=0.62,            # markedly smaller — the frame IS the device
    shake=1.5,
)

LOOKS: dict[str, Look] = {
    look.name: look for look in (BROADCAST, OPERATOR, UNDERGROUND, FOUND_FOOTAGE)
}


def resolve(name: str | None, overrides: dict[str, Any] | None = None) -> Look | None:
    """Look up a format by name, applying any per-scenario overrides.

    An unknown name is an error rather than a silent fallback to "no look": a typo in
    the frontmatter would otherwise render a plain video that looks like the format was
    simply not very strong, which is the hardest kind of mistake to notice.
    """
    if not name:
        return None
    base = LOOKS.get(name)
    if base is None:
        raise KeyError(
            f"unknown look {name!r} — available: {', '.join(sorted(LOOKS))}"
        )
    if not overrides:
        return base
    unknown = set(overrides) - {f for f in base.__dataclass_fields__}
    if unknown:
        raise KeyError(
            f"look {name!r} has no setting(s) {', '.join(sorted(unknown))} — "
            f"available: {', '.join(sorted(base.__dataclass_fields__))}"
        )
    return Look(**{**base.__dict__, **overrides})


def from_spec(spec: dict[str, Any]) -> Look:
    """Build a one-off look from a mapping — for a project that is not this one.

    :data:`LOOKS` is a closed set of *this* universe's formats: its tests assert the four
    names, the two registers and the ``SCH-`` art presets, and those assertions are the
    thing keeping a look and the prompt library from drifting apart. A different project
    needs its own grade without joining that set and without loosening those guarantees,
    so it passes the numbers directly and gets a :class:`Look` that is never registered.

    An unknown key is an error for the same reason a typo'd look name is: a silently
    ignored setting renders a video that merely looks weak, which is the hardest kind of
    mistake to notice.
    """
    if not isinstance(spec, dict):
        raise TypeError(f"a look spec must be a mapping of setting to value, got {type(spec).__name__}")
    fields = set(Look.__dataclass_fields__)
    unknown = set(spec) - fields
    if unknown:
        raise KeyError(
            f"a look has no setting(s) {', '.join(sorted(unknown))} — "
            f"available: {', '.join(sorted(fields))}"
        )
    if not spec.get("name"):
        raise KeyError("a look spec needs a `name` — it is what the render log calls it")
    return Look(**{"description": "", **spec})
