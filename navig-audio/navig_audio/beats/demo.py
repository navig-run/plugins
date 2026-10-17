"""Guide-vocal demos — hear a hook on a beat before anyone records it.

A demo sheet lists the hook's lines and which *character* says each one; a voices file says
what each character sounds like: a stock text-to-speech voice, a pitch shift in semitones
(down for a giant, up for a chipmunk), whether the formants move with it (``shift`` is the
cartoon mask, ``keep`` stays human), and a few effects (room, hall, grit, robot, radio,
tape, double). Each line is spoken, shaped, and dropped onto its bar of the **measured**
bar map; the beat is side-chain ducked under the voice; a 30-second excerpt is cut with
fades and tagged ``GUIDE — NOT FOR RELEASE``.

This is a sketch for choosing a language × beat pair, not a vocal. It never clones anyone.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from navig_audio.beats.layer import FINITE, FORMAT

GUIDE_TAG = "GUIDE — NOT FOR RELEASE"

# A guide teaches the words, so the voice sits ON the beat. The bed used to be the raw beat
# minus 4 dB and the voice the raw TTS: on a loud beat (-9 LUFS) the voice landed 9-13 LU
# under it (measured by splitting three demos, 3 Oct 2026). Both are now measured and set:
# re-split, the same demos put the voice 4.5-6 LU over the ducked beat (-20 here gave 7-8,
# and the beat all but vanished under a dense verse).
BED_LEVEL = -17.0  # LUFS of the beat excerpt before ducking
VOICE_LEVEL = -16.0  # LUFS of each spoken line after its mask
MAX_LEVEL_GAIN = 24.0  # never boost a near-silent clip into noise
DEFAULT_TTS_MODEL = "eleven_multilingual_v2"

# Effect presets, in the order they are applied. Plain ffmpeg filters, nothing exotic.
FX: dict[str, str] = {
    "room": "aecho=0.8:0.6:35:0.25",
    "hall": "aecho=0.8:0.8:90|180:0.35|0.25",
    "grit": "acrusher=bits=10:mix=0.3",
    "robot": "flanger=delay=2:depth=3:regen=40:speed=0.35,acrusher=bits=8:mix=0.35",
    "radio": "highpass=f=300,lowpass=f=3400",
    "tape": "vibrato=f=0.7:d=0.08",
    "double": "chorus=0.6:0.9:40|60:0.4|0.3:0.25|0.4:2|1.3",
    "whisper": "highpass=f=250,treble=g=4",
}


class DemoError(ValueError):
    """A demo sheet or voices file the renderer cannot use."""


@dataclass
class Voice:
    key: str
    voice_id: str
    label: str = ""
    model: str = DEFAULT_TTS_MODEL
    pitch: float = 0.0  # semitones
    formant: str = "keep"  # keep | shift
    fx: list[str] = field(default_factory=list)
    gain_db: float = 0.0
    settings: dict[str, Any] | None = None

    def chain(self) -> list[str]:
        parts: list[str] = []
        if self.pitch:
            ratio = 2 ** (self.pitch / 12.0)
            formant = "preserved" if self.formant == "keep" else "shifted"
            parts.append(f"rubberband=pitch={ratio:.5f}:formant={formant}")
        for name in self.fx:
            if name not in FX:
                raise DemoError(f"voice {self.key!r}: unknown effect {name!r} — known: {', '.join(FX)}")
            parts.append(FX[name])
        if self.gain_db:
            parts.append(f"volume={self.gain_db:.1f}dB")
        return parts


def load_voices(data: dict[str, Any]) -> dict[str, Voice]:
    raw = data.get("voices", data)
    if not isinstance(raw, dict) or not raw:
        raise DemoError("the voices file has no voices")
    out: dict[str, Voice] = {}
    for key, v in raw.items():
        if not isinstance(v, dict) or not v.get("voice_id"):
            raise DemoError(f"voice {key!r} needs a voice_id")
        out[key] = Voice(
            key=key, voice_id=str(v["voice_id"]), label=str(v.get("label", key)),
            model=str(v.get("model", DEFAULT_TTS_MODEL)), pitch=float(v.get("pitch", 0)),
            formant=str(v.get("formant", "keep")), fx=[str(x) for x in v.get("fx", [])],
            gain_db=float(v.get("gain_db", 0)), settings=v.get("settings"),
        )
    return out


@dataclass
class Placed:
    index: int
    text: str
    voice: str
    bar: int  # absolute bar in the bar map
    at_s: float  # seconds into the excerpt
    clip: Path
    clip_s: float | None = None

    def to_dict(self, slot_s: float, rate: float = 1.0) -> dict[str, Any]:
        """``slot_s`` is the time the line OWNS (bar × bars_per_line) — comparing against one
        bar called every line of a two-bar-per-line demo an overrun."""
        d: dict[str, Any] = {"line": self.index + 1, "voice": self.voice, "bar": self.bar,
                             "at_s": round(self.at_s, 3), "text": self.text}
        if self.clip_s is not None:
            d["clip_s"] = round(self.clip_s, 2)
            if rate > 1.0:
                d["fit_rate"] = round(rate, 2)
            d["overruns_bar"] = self.clip_s / rate > slot_s * 1.05
        return d


def tts_cache_name(voice: Voice, text: str) -> str:
    digest = hashlib.sha1(f"{voice.voice_id}|{voice.model}|{text}".encode("utf-8")).hexdigest()[:12]
    return f"tts-{digest}.mp3"


@dataclass
class DemoPlan:
    start_s: float  # excerpt start in the beat
    length_s: float
    bar_s: float
    placed: list[Placed]
    slot_s: float = 0.0  # the time one line owns (bar_s × bars_per_line)
    fit: bool = False  # time-compress a spoken line that overruns its slot, pitch kept

    def fit_rate(self, p: Placed) -> float:
        """How much faster this line must play to fit its slot (1.0 = as spoken).

        Stock TTS reads at narration pace (~3 syllables/s); a rap line asks 3.5–5. Without
        this a guide demo teaches the wrong pace — every line spills into the next bar.
        Capped at 1.8×: beyond that the guide stops sounding like a voice.
        """
        if not self.fit or not p.clip_s or not self.slot_s or p.clip_s <= self.slot_s * 0.98:
            return 1.0
        return min(1.8, p.clip_s / (self.slot_s * 0.95))


def plan_demo(sheet: dict[str, Any], voices: dict[str, Voice], bar_map, cache_dir: Path) -> DemoPlan:
    lines = sheet.get("lines") or []
    if not lines:
        raise DemoError("the demo sheet has no lines")
    if sheet.get("start_block"):
        start_bar = bar_map.block(str(sheet["start_block"])).start_bar
    elif sheet.get("start_bar"):
        start_bar = int(sheet["start_bar"])
    else:
        raise DemoError("the demo sheet needs start_bar or start_block")
    lead_in = int(sheet.get("lead_in_bars", 2))
    per_line = int(sheet.get("bars_per_line", 1))
    length = float(sheet.get("length_s", 30.0))
    excerpt_start = max(0.0, bar_map.bar_time(start_bar - lead_in))
    placed: list[Placed] = []
    for i, ln in enumerate(lines):
        if isinstance(ln, str):
            ln = {"text": ln}
        voice_key = str(ln.get("voice") or sheet.get("voice") or "")
        if voice_key not in voices:
            raise DemoError(f"line {i + 1}: unknown voice {voice_key!r} — known: {', '.join(voices)}")
        bar = start_bar + int(ln["bar"]) if "bar" in ln else start_bar + i * per_line
        at = bar_map.bar_time(bar) - excerpt_start + float(ln.get("offset_beats", 0)) * bar_map.bar_s / 4
        if at >= length:
            break  # past the end of the 30 s window — not rendered, not paid for
        placed.append(Placed(index=i, text=str(ln["text"]), voice=voice_key, bar=bar, at_s=at,
                             clip=cache_dir / tts_cache_name(voices[voice_key], str(ln["text"]))))
    return DemoPlan(start_s=excerpt_start, length_s=length, bar_s=bar_map.bar_s, placed=placed,
                    slot_s=bar_map.bar_s * per_line, fit=bool(sheet.get("fit_to_bar", False)))


def level_gain(measured_lufs: float | None, target: float) -> float:
    """dB that takes a measured loudness to ``target``; 0 for silence or an unmeasured file."""
    if measured_lufs is None or not math.isfinite(measured_lufs):
        return 0.0
    return max(-MAX_LEVEL_GAIN, min(MAX_LEVEL_GAIN, target - measured_lufs))


def build_graph(plan: DemoPlan, voices: dict[str, Voice], *, bed_db: float = -4.0,
                fade_in: float = 0.5, fade_out: float = 2.0, lufs: float = -14.0,
                line_gain_db: list[float] | None = None) -> str:
    """Input 0 is the beat excerpt; inputs 1..n the spoken lines in plan order.

    ``line_gain_db`` (one per placed line) levels each spoken line after its mask; ``bed_db``
    sets the beat. :func:`render` measures both so the voice lands where a guide needs it.
    """
    from navig_media_core.media.audio_edit import tempo_filter  # pitch-keeping atempo chain

    chains = [f"[0:a]{FORMAT},volume={bed_db:.1f}dB[bed]"]
    vox = []
    for i, p in enumerate(plan.placed, start=1):
        ms = int(round(p.at_s * 1000))
        rate = plan.fit_rate(p)
        tempo = [tempo_filter(rate)] if rate > 1.0 else []
        level = line_gain_db[i - 1] if line_gain_db else 0.0
        trim = [f"volume={level:.1f}dB"] if level else []
        parts = [f"[{i}:a]{FORMAT}", *tempo, *voices[p.voice].chain(), *trim, f"adelay={ms}|{ms}"]
        chains.append(",".join(parts) + f"[v{i}]")
        vox.append(f"[v{i}]")
    # The voice bus is padded to the whole excerpt: sidechaincompress stops when its side
    # chain stops, so a hook that ends early would otherwise cut the demo short.
    pad = f"apad=whole_dur={plan.length_s:.3f}"
    if len(vox) == 1:
        chains.append(f"{vox[0]}{pad},{FINITE}[vox]")
    else:
        chains.append(f"{''.join(vox)}amix=inputs={len(vox)}:normalize=0,{pad},{FINITE}[vox]")
    chains.append("[vox]asplit=2[vx][sc]")
    chains.append("[bed][sc]sidechaincompress=threshold=0.03:ratio=6:attack=15:release=300[duck]")
    st = max(0.0, plan.length_s - fade_out)
    chains.append(
        f"[duck][vx]amix=inputs=2:normalize=0:duration=first,"
        f"afade=t=in:st=0:d={fade_in:.3f},afade=t=out:st={st:.3f}:d={fade_out:.3f},"
        f"{FINITE},loudnorm=I={lufs:g}:TP=-1.0:LRA=11[out]"
    )
    return ";".join(chains)


def render(beat: Path, plan: DemoPlan, voices: dict[str, Voice], dst: Path, *,
           title: str, timeout: int = 600) -> Path:
    from navig_media_core.media.audio_edit import _exec_ffmpeg, _require_ffmpeg

    bed_db, line_gains = measure_levels(beat, plan, voices, timeout=timeout)
    graph = build_graph(plan, voices, bed_db=bed_db, line_gain_db=line_gains)
    exe = _require_ffmpeg()
    cmd = [exe, "-nostdin", "-y", "-ss", f"{plan.start_s:.3f}", "-t", f"{plan.length_s:.3f}",
           "-i", str(beat)]
    for p in plan.placed:
        cmd += ["-i", str(p.clip)]
    cmd += ["-filter_complex", graph, "-map", "[out]", "-t", f"{plan.length_s:.3f}",
            "-ar", "44100", "-c:a", "libmp3lame", "-q:a", "2",
            "-metadata", f"title={title}", "-metadata", f"comment={GUIDE_TAG}", str(dst)]
    _exec_ffmpeg(cmd, dst, timeout, dst.name)
    return dst


def measure_levels(beat: Path, plan: DemoPlan, voices: dict[str, Voice], *,
                   timeout: int = 600) -> tuple[float, list[float]]:
    """(bed gain, per-line gains) that put the excerpt at BED_LEVEL and each line at VOICE_LEVEL.

    Each line is measured AFTER its mask (pitch and effects change its loudness) and its
    speed-up, exactly as it will be mixed.
    """
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    from navig_audio.mastering.loudness import measure  # noqa: PLC0415
    from navig_media_core.media.audio_edit import _require_ffmpeg, tempo_filter  # noqa: PLC0415

    exe = _require_ffmpeg()
    with tempfile.TemporaryDirectory(prefix="navig-demo-") as tmp:
        def render_and_measure(args: list[str], name: str) -> float | None:
            out = Path(tmp) / name
            subprocess.run([exe, "-nostdin", "-y", "-v", "error", *args, str(out)],
                           check=False, capture_output=True, timeout=timeout)
            return measure(out, timeout=timeout).lufs if out.exists() else None

        bed = render_and_measure(["-ss", f"{plan.start_s:.3f}", "-t", f"{plan.length_s:.3f}",
                                  "-i", str(beat), "-af", FORMAT], "bed.wav")
        gains = []
        for i, p in enumerate(plan.placed):
            rate = plan.fit_rate(p)
            chain = [FORMAT, *([tempo_filter(rate)] if rate > 1.0 else []), *voices[p.voice].chain()]
            gains.append(level_gain(render_and_measure(["-i", str(p.clip), "-af", ",".join(chain)],
                                                       f"line{i}.wav"), VOICE_LEVEL))
    bed_db = level_gain(bed, BED_LEVEL) if bed is not None and math.isfinite(bed) else -4.0
    return bed_db, gains


def semitones_label(pitch: float) -> str:
    if not pitch:
        return "natural"
    return f"{'+' if pitch > 0 else '−'}{abs(pitch):g} st"


def ratio(pitch: float) -> float:
    return math.pow(2.0, pitch / 12.0)
