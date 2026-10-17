"""The timeline: when each word is said, and where the picture cuts.

Ported from the channel workspace's ``kinetic.py`` — the timing contract is
the voice's own (ElevenLabs word timestamps), never guessed, and the cut grid
comes from the bed so cuts land on the music. Voice lines come from
``navig_audio.voice.line`` (shared cache), speech grouping from
``navig_audio.speech``, the bed from ``navig_audio.beds`` and the grid from
``navig_audio.bedgrid`` — the same engines the workspace scripts now share.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from navig_video.kinetic.ctx import ctx
from navig_video.workspace import load_workspace_module

__all__ = ["Beat", "END_HOLD", "LEAD_IN", "OPEN_HOLD", "Timeline", "plan",
           "red_mask", "regroup"]

LEAD_IN = 0.12
OPEN_HOLD = 3.0                  # the standard title open owns [0, LEAD_IN + OPEN_HOLD)
END_HOLD = 2.2


@dataclass
class Beat:
    t0: float
    t1: float
    plate: str
    cut: str
    words: list[dict]            # {w, s, e, red} with absolute times
    audio: Path
    sfx: str | None
    seg: int = 0                 # index of the continuous shot it belongs to
    fx: str | None = None        # "interference": the line arrives through static
    bar: str | None = None       # "lead" | "full": the loading bar under the words


@dataclass
class Timeline:
    beats: list[Beat] = field(default_factory=list)
    end_t0: float = 0.0
    total: float = 0.0
    open_until: float = 0.0      # the title open runs [0, open_until); 0 = none
    vals: dict = field(default_factory=dict)
    lang: str = "en"
    motion: str | None = None    # card-style rise ("rise") for neighbour


def bed_for(name: str, ep: dict, workspace: Path) -> tuple[Path, float]:
    """The episode's own track: matched to its mood, never shared."""
    from navig_audio.beds import assign, mood_for

    return assign(name, mood_for(name, ep), workspace)


def plan(name: str, lang: str, vals: dict) -> Timeline:
    """Build the timeline: one beat per beat, words at their spoken times."""
    from navig_audio.bedgrid import grid
    from navig_audio.speech import groups as sp_groups
    from navig_audio.voice.line import duration, synth_timed

    ws = ctx().ws
    EP = load_workspace_module(ws, "episodes")
    ep = EP.EPISODES[name]
    voice = EP.VOICES[lang]
    open_until = LEAD_IN + OPEN_HOLD       # the cold open, then the first beat
    tl, t, plate, seg = Timeline(open_until=open_until), open_until, None, -1
    bed, bed_at = bed_for(name, ep, ws)
    beats = grid(bed, bed_at, 60, ctx().sound / "grids")

    def on_beat(x: float) -> float:
        """The next beat at or just before x — a cut lands on the music.
        Never pushes a cut more than 0.45 s, so a written pause survives."""
        for g in beats:
            if g >= x - 0.06:
                return g if g - x <= 0.45 else x
        return x

    for b in ep["beats"]:
        text = b["say"][lang].format(**vals)
        # sayv: the voice may speak a different line than the screen shows
        # (Null Island → Nuhl Island on air); token counts must match so the
        # regroup folds the spoken words back onto the display tokens
        vtext = (b.get("sayv") or {}).get(lang, text)
        disp = _clean(text).split()
        grp = sp_groups(_clean(vtext).split(), lang)
        v = EP.VOICES2.get(lang, voice) if b.get("voice") == 2 else voice
        result = synth_timed(" ".join(w for g in grp for w in g), v, lang, ctx().voc)
        mp3, spoken = result.path, result.words
        words = regroup(disp, grp, spoken)
        red = red_mask(text)
        d = duration(mp3)
        if b.get("plate") and b["plate"] != plate:
            plate, seg = b["plate"], seg + 1
        ws_words = [{"w": w["w"], "s": t + w["s"], "e": t + w["e"],
                     "red": red[i] if i < len(red) else False}
                    for i, w in enumerate(words)]
        cut = b.get("cut", "hard")
        sfx = b.get("sfx") or ("hit_low" if cut in ("hard", "glitch") and tl.beats else None)
        t1 = on_beat(t + d + float(b.get("pause", 0.35)))
        tl.beats.append(Beat(t, t1, plate, cut, ws_words, mp3, sfx, seg,
                             b.get("fx"), b.get("bar")))
        t = t1
    tl.end_t0 = t
    tl.total = t + END_HOLD
    tl.vals = vals
    tl.lang = lang
    tl.motion = ep.get("motion")          # card-style rise for neighbour
    return tl


def _clean(text: str) -> str:
    return text.replace("*", "")


def regroup(disp: list[str], groups: list[list[str]], spoken: list[dict]) -> list[dict]:
    """Fold the spoken words back onto the display tokens: "266" starts when
    "двести" starts and ends when "шесть" ends. If the counts disagree (the
    model split a word differently) the spoken words are shown as they are."""
    if sum(len(g) for g in groups) != len(spoken):
        return spoken
    out, i = [], 0
    for tok, g in zip(disp, groups):
        seg = spoken[i:i + len(g)]
        out.append({"w": tok, "s": seg[0]["s"], "e": seg[-1]["e"]})
        i += len(g)
    return out


def red_mask(text: str) -> list[bool]:
    """Which spoken words were wrapped in *asterisks*."""
    out, inside = [], False
    for tok in text.split():
        starts = tok.startswith("*") or tok.startswith("«*") or tok.startswith("(*")
        if starts:
            inside = True
        out.append(inside)
        if tok.rstrip(".,:;!?»—").endswith("*"):
            inside = False
    return out
