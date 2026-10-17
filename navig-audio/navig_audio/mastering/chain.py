"""The filter graphs: a vocal chain, a beat that makes room for it, and the glue on the mix.

A preset is data, not code: every number a mastering engineer would turn lives in
:data:`PRESETS`, and :func:`build_mix_graph` only assembles them. The chain is the common
rap-vocal chain — clean the lows, take the boxiness out, compress, add presence and air,
tame the esses — and the beat answers by dipping where the voice speaks (2–4 kHz) and by
ducking under it. Both inputs are levelled first, so the compressor thresholds below mean
the same thing whether the take was recorded hot or quiet.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The shared sample format of every branch (the same one beats/layer.py mixes in).
FORMAT = "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo"


@dataclass(frozen=True)
class Preset:
    name: str
    about: str
    lufs: float  # the master's integrated loudness target
    vocal_level: float = -20.0  # LUFS the raw take is levelled to before the chain
    # How far the processed vocal sits above the beat (LU, before ducking). The beat's level is
    # *derived* from the measured vocal (master.py), so a quiet or hot take lands the same.
    balance_lu: float = 2.5
    beat_level: float = -22.0  # fallback only, when the vocal was not measured
    highpass: int = 90
    mud: tuple[int, float] = (280, -3.0)  # Hz, dB — boxiness
    presence: tuple[int, float] = (3000, 2.5)  # Hz, dB — intelligibility
    air: tuple[int, float] = (9000, 2.0)  # high shelf
    comp: dict = field(default_factory=lambda: {"threshold": 0.08, "ratio": 4, "attack": 10,
                                                "release": 120, "makeup": 2.5})
    deess: float = 0.4  # deesser intensity 0..1
    drive_db: float = 2.0  # into a tanh soft clip: warmth, not distortion
    space: str = ""  # ffmpeg filters appended to the vocal (delay / short reverb)
    beat_carve: tuple[int, float] = (3000, -2.0)  # the beat makes room where the voice speaks
    duck_ratio: float = 2.5  # sidechain ratio: how hard the beat ducks under the voice
    glue: dict = field(default_factory=lambda: {"threshold": 0.3, "ratio": 2, "attack": 30,
                                                "release": 250, "makeup": 1})


# Echo/space: ``aecho`` with in_gain 1 and out_gain 1 keeps the dry voice at full level and
# adds the reflections on top. (out_gain < 1 scales the WHOLE signal: 0.5 then 0.4 cost the
# voice 14 dB and buried it 19 LU under the beat — measured, 3 Oct 2026.)
PRESETS: dict[str, Preset] = {
    # balance / duck calibrated 3 Oct 2026 against released mixes: split by Demucs, an ATL track
    # puts the voice 1.4-3.1 LU UNDER the music (median -2.5). Re-splitting our own masters,
    # balance -3 with a light duck (1.2) lands the voice -1.6…-3.3 LU under the music across
    # two songs; the first presets (+2…+3, duck 2-3) put it +6.4 LU over — "the voice too loud".
    "ritual": Preset(
        name="ritual",
        about="close, incantatory voice INSIDE a dense rave/jungle beat; slapback + short plate",
        lufs=-9.0, balance_lu=-3.0, presence=(3200, 3.0), drive_db=3.0, duck_ratio=1.2,
        space="aecho=1:1:110:0.18,aecho=1:1:23|41|67:0.10|0.08|0.06",
    ),
    "boom-bap": Preset(
        name="boom-bap",
        about="dry voice a little forward over a dusty boom bap; a small room",
        lufs=-10.0, balance_lu=-2.0, presence=(2800, 2.5), drive_db=2.5, duck_ratio=1.5,
        space="aecho=1:1:17|29:0.08|0.05",
    ),
    "collage": Preset(
        name="collage",
        about="sample-collage breakbeat; the voice in the collage, a short plate around it",
        lufs=-10.0, balance_lu=-3.0, presence=(3000, 2.0), air=(10000, 1.5), duck_ratio=1.5,
        space="aecho=1:1:31|53|79:0.10|0.08|0.05",
    ),
    "clean": Preset(
        name="clean",
        about="demos, podcasts, guides: transparent chain, broadcast loudness",
        lufs=-14.0, balance_lu=3.0, presence=(3000, 1.5), air=(10000, 1.0), drive_db=0.0, duck_ratio=2.0,
        comp={"threshold": 0.1, "ratio": 3, "attack": 15, "release": 150, "makeup": 1.8},
    ),
}


# Voice colour, applied after the chain (or after levelling, for a --vocal-ready take). A dark
# beat has almost nothing above 2 kHz (Session 55's: 0.4 % of its energy) while a DAW-processed
# voice keeps ~12 % there — it sits ON the beat however low it is. Rolling the top off and adding
# a short room tail puts it IN the beat. Both picked by ear on Session 55 (3 Oct 2026) with
# --vocal-ready: `dark` at --balance -5.5, `darker` at --balance -7.5.
VOICE_TONES: dict[str, str] = {
    "dark": "lowpass=f=7000:poles=2,highshelf=f=3500:g=-3,aecho=1:1:19|33:0.10|0.07",
    "darker": "lowpass=f=6000:poles=2,highshelf=f=3000:g=-4,aecho=1:1:19|33|51:0.12|0.09|0.06",
}


def voice_tone(name: str | None) -> str | None:
    """The filter of a named voice tone; None (or "none") keeps the voice as the chain left it."""
    if name in (None, "", "none"):
        return None
    try:
        return VOICE_TONES[name]
    except KeyError:
        raise ValueError(f"unknown voice tone {name!r} — one of: none, {', '.join(VOICE_TONES)}") from None


def preset(name: str) -> Preset:
    try:
        return PRESETS[name]
    except KeyError:
        raise ValueError(f"unknown preset {name!r} — one of: {', '.join(PRESETS)}") from None


def vocal_chain(p: Preset, *, denoise: bool = True, gain_db: float = 0.0, ready: bool = False,
                tone: str | None = None) -> str:
    """The rap-vocal chain — or, with ``ready``, only levelling.

    ``ready`` is for a voice already processed in a DAW (compressed, EQ'd, effected): running
    the chain again stacks a second compressor, presence boost and echo on the first, which
    pushes the voice forward and hardens it. Then the take is only levelled and blended.

    ``tone`` (a :data:`VOICE_TONES` name) colours the voice last, after any trim.
    """
    colour = voice_tone(tone)
    if ready:
        steps = [FORMAT, f"loudnorm=I={p.vocal_level}:TP=-3:LRA=11", FORMAT]
        if gain_db:
            steps.append(f"volume={gain_db}dB")
        if colour:
            steps.append(colour)
        return ",".join(steps)
    c = p.comp
    steps = [
        FORMAT,
        f"loudnorm=I={p.vocal_level}:TP=-3:LRA=11",  # level the take so the thresholds mean something
        FORMAT,
        f"highpass=f={p.highpass}:poles=2",
    ]
    if denoise:
        steps.append("afftdn=nr=10:nf=-45:tn=1")  # conservative: room hiss, not the voice
    steps += [
        f"equalizer=f={p.mud[0]}:t=q:w=1.2:g={p.mud[1]}",
        f"acompressor=threshold={c['threshold']}:ratio={c['ratio']}:attack={c['attack']}"
        f":release={c['release']}:makeup={c['makeup']}",
        f"deesser=i={p.deess}:m=0.5:f=0.5",
        f"equalizer=f={p.presence[0]}:t=q:w=1.0:g={p.presence[1]}",
        f"highshelf=f={p.air[0]}:g={p.air[1]}",
    ]
    if p.drive_db > 0:
        steps += [f"volume={p.drive_db}dB", "asoftclip=type=tanh", f"volume={-p.drive_db}dB"]
    if p.space:
        steps.append(p.space)
    if gain_db:
        steps.append(f"volume={gain_db}dB")
    if colour:
        steps.append(colour)
    return ",".join(steps)


def beat_chain(p: Preset, level: float | None = None) -> str:
    return ",".join([
        FORMAT,
        f"loudnorm=I={p.beat_level if level is None else level:.2f}:TP=-2:LRA=11",
        FORMAT,
        f"equalizer=f={p.beat_carve[0]}:t=q:w=0.8:g={p.beat_carve[1]}",
    ])


def build_mix_graph(p: Preset, *, offset_s: float = 0.0, vocal_gain_db: float = 0.0,
                    duck_ratio: float | None = None, denoise: bool = True,
                    total_s: float | None = None, beat_level: float | None = None,
                    ready: bool = False, tone: str | None = None) -> str:
    """``[0:a]`` = vocal, ``[1:a]`` = beat → ``[mix]``, the glued pre-master.

    ``total_s`` is the song's length (the longer of the beat and offset + vocal). The vocal is
    padded with silence to it: ``sidechaincompress`` stops at the end of its *key*, so an
    unpadded take would cut the beat off the moment the voice ends.
    """
    ratio = p.duck_ratio if duck_ratio is None else duck_ratio
    delay = max(0, round(offset_s * 1000))
    g = p.glue
    vocal = vocal_chain(p, denoise=denoise, gain_db=vocal_gain_db, ready=ready, tone=tone)
    if delay:
        vocal += f",adelay=delays={delay}:all=1"
    if total_s:
        vocal += f",apad=whole_dur={total_s:.3f}"
    parts = [
        f"[0:a]{vocal},asplit=2[vmix][vkey]",
        f"[1:a]{beat_chain(p, beat_level)}[bed]",
    ]
    # The beat ducks only while the voice is there; ratio 1 turns the duck off.
    if ratio > 1:
        parts.append(f"[bed][vkey]sidechaincompress=threshold=0.04:ratio={ratio}:attack=8:release=220:makeup=1[ducked]")
    else:
        parts += ["[bed]anull[ducked]", "[vkey]anullsink"]
    parts += [
        "[ducked][vmix]amix=inputs=2:duration=longest:normalize=0,"
        f"acompressor=threshold={g['threshold']}:ratio={g['ratio']}:attack={g['attack']}"
        f":release={g['release']}:makeup={g['makeup']},{FORMAT}[mix]",
    ]
    return ";".join(parts)
