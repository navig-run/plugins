"""Layer sound effects onto a beat — at bars, with fades — without touching the dry beat.

A layer sheet says *what* (an ElevenLabs sound-effect prompt, or a file), *where* (a bar,
a measured block such as ``drop`` or ``tail``, or seconds), *how loud* and *how it comes
and goes* (fade in / fade out). Positions in bars are converted through the beat's measured
:class:`~navig_audio.beats.bars.BarMap`, so an effect meant for "the drop" lands on the
drop the file actually has, not the one the generation plan asked for.

The mix is one ffmpeg pass: each occurrence is trimmed, faded, set to its gain and delayed
to its start, then everything is summed with ``amix=normalize=0`` (so the beat is not
quietly turned down by the number of layers), faded in and out as a whole, and brought to
a streaming loudness with ``loudnorm``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_LUFS = -14.0
FORMAT = "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo"
# A single NaN sample — measured: rubberband pitch-shift followed by vibrato emits one — is
# spread by loudnorm across the whole output, and LAME then aborts ("psymodel.c: el >= 0").
# Every mix scrubs non-finite samples to silence before it is normalised.
FINITE = ("aeval=exprs='if(isnan(val(0))+isinf(val(0)),0,val(0))|"
          "if(isnan(val(1))+isinf(val(1)),0,val(1))':channel_layout=stereo")


class LayerError(ValueError):
    """A layer sheet the mixer cannot use."""


@dataclass
class Occurrence:
    """One placed sound: file, start in the beat, and how it is shaped."""

    name: str
    file: Path
    start_s: float
    length_s: float | None = None
    gain_db: float = -12.0
    fade_in: float = 0.0
    fade_out: float = 0.0

    def chain(self, label: str, index: int) -> str:
        parts = [f"[{index}:a]{FORMAT}"]
        if self.length_s:
            parts.append(f"atrim=0:{self.length_s:.3f},asetpts=PTS-STARTPTS")
        if self.fade_in > 0:
            parts.append(f"afade=t=in:st=0:d={self.fade_in:.3f}")
        if self.fade_out > 0 and self.length_s:
            st = max(0.0, self.length_s - self.fade_out)
            parts.append(f"afade=t=out:st={st:.3f}:d={self.fade_out:.3f}")
        parts.append(f"volume={self.gain_db:.1f}dB")
        ms = int(round(self.start_s * 1000))
        parts.append(f"adelay={ms}|{ms}")
        return ",".join(parts) + f"[{label}]"


@dataclass
class Master:
    fade_in: float = 1.0
    fade_out: float = 3.0
    lufs: float | None = DEFAULT_LUFS


def build_graph(duration_s: float, occurrences: list[Occurrence], master: Master) -> str:
    """The ``-filter_complex`` string: input 0 is the beat, inputs 1..n the occurrences."""
    chains = [f"[0:a]{FORMAT}[bed]"]
    labels = ["[bed]"]
    for i, occ in enumerate(occurrences, start=1):
        chains.append(occ.chain(f"l{i}", i))
        labels.append(f"[l{i}]")
    tail = [f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0:duration=first:dropout_transition=0"]
    if master.fade_in > 0:
        tail.append(f"afade=t=in:st=0:d={master.fade_in:.3f}")
    if master.fade_out > 0:
        st = max(0.0, duration_s - master.fade_out)
        tail.append(f"afade=t=out:st={st:.3f}:d={master.fade_out:.3f}")
    if master.lufs is not None:
        tail.append(FINITE)
        tail.append(f"loudnorm=I={master.lufs:g}:TP=-1.0:LRA=11")
    chains.append(",".join(tail) + "[out]")
    return ";".join(chains)


def sfx_cache_name(prompt: str, duration: float | None) -> str:
    digest = hashlib.sha1(f"{prompt}|{duration or ''}".encode("utf-8")).hexdigest()[:12]
    return f"sfx-{digest}.mp3"


def resolve_start(spec: dict[str, Any], bar_map) -> float:
    """Seconds into the beat for one layer entry (``at_s`` / ``at_bar`` / ``block``)."""
    if "at_s" in spec:
        return float(spec["at_s"])
    offset_bars = float(spec.get("offset_bars", 0))
    if "block" in spec:
        if bar_map is None:
            raise LayerError(f"layer {spec.get('name')!r} uses a block but no bar map is available")
        block = bar_map.block(str(spec["block"]))
        edge = str(spec.get("edge", "start"))
        base = block.start_s if edge == "start" else block.end_s
        return base + offset_bars * bar_map.bar_s
    if "at_bar" in spec:
        if bar_map is None:
            raise LayerError(f"layer {spec.get('name')!r} uses at_bar but no bar map is available")
        return bar_map.bar_time(int(spec["at_bar"])) + offset_bars * bar_map.bar_s
    raise LayerError(f"layer {spec.get('name')!r} needs at_s, at_bar or block")


@dataclass
class LayerPlan:
    occurrences: list[Occurrence] = field(default_factory=list)
    to_generate: list[tuple[str, float | None, Path]] = field(default_factory=list)  # prompt, dur, cache path


def plan_layers(sheet: dict[str, Any], *, bar_map, cache_dir: Path, base_dir: Path) -> LayerPlan:
    """Turn a layer sheet into placed occurrences (+ the SFX that still need generating)."""
    layers = sheet.get("layers") or []
    if not isinstance(layers, list) or not layers:
        raise LayerError("the sheet has no layers")
    plan = LayerPlan()
    queued: set[Path] = set()
    for spec in layers:
        name = str(spec.get("name") or f"layer{len(plan.occurrences) + 1}")
        if "file" in spec:
            src = Path(spec["file"])
            src = src if src.is_absolute() else (base_dir / src)
            if not src.exists():
                raise LayerError(f"layer {name!r}: file not found: {src}")
        elif "prompt" in spec:
            dur = float(spec["duration"]) if spec.get("duration") else None
            src = cache_dir / sfx_cache_name(str(spec["prompt"]), dur)
            if not src.exists() and src not in queued:
                plan.to_generate.append((str(spec["prompt"]), dur, src))
                queued.add(src)
        else:
            raise LayerError(f"layer {name!r} needs a prompt or a file")
        length = spec.get("length_s")
        if length is None and spec.get("length_bars") and bar_map is not None:
            length = float(spec["length_bars"]) * bar_map.bar_s
        start = resolve_start(spec, bar_map)
        repeat = int(spec.get("repeat", 1))
        every = float(spec.get("every_bars", 0)) * (bar_map.bar_s if bar_map else 0.0)
        for k in range(max(1, repeat)):
            plan.occurrences.append(Occurrence(
                name=name if repeat <= 1 else f"{name}#{k + 1}",
                file=src, start_s=start + k * every,
                length_s=float(length) if length else None,
                gain_db=float(spec.get("gain_db", -12.0)),
                fade_in=float(spec.get("fade_in", 0.0)),
                fade_out=float(spec.get("fade_out", 0.0)),
            ))
    return plan


def mix(beat: Path, occurrences: list[Occurrence], dst: Path, master: Master, *,
        title: str | None = None, comment: str | None = None, timeout: int = 600) -> Path:
    """Render the layered beat to ``dst`` (mp3)."""
    from navig_generate.media.audio_edit import _exec_ffmpeg, _require_ffmpeg, probe_duration

    duration = probe_duration(beat)
    graph = build_graph(duration, occurrences, master)
    exe = _require_ffmpeg()
    cmd = [exe, "-nostdin", "-y", "-i", str(beat)]
    for occ in occurrences:
        cmd += ["-i", str(occ.file)]
    cmd += ["-filter_complex", graph, "-map", "[out]", "-ar", "44100",
            "-c:a", "libmp3lame", "-q:a", "2"]
    if title:
        cmd += ["-metadata", f"title={title}"]
    if comment:
        cmd += ["-metadata", f"comment={comment}"]
    cmd.append(str(dst))
    _exec_ffmpeg(cmd, dst, timeout, dst.name)
    return dst
