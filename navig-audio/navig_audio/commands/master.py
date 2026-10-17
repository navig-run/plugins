"""``navig audio master`` / ``navig audio loudness`` — a vocal over a beat, out as a finished song.

  navig audio loudness song.mp3 ref.mp3                    LUFS · loudness range · true peak
  navig audio master vocal.wav --beat beat.mp3             mix + master (preset: ritual, -9 LUFS)
  navig audio master vocal.wav --beat beat.mp3 --preset boom-bap --offset 4.2 --wav
  navig audio master vocal.wav --beat beat.mp3 --ref reference.mp3   match a reference (matchering)

Mounted on ``audio_app`` by import side-effect from ``audio.py``, like ``beat`` and ``episode``.
"""

from __future__ import annotations

import json as _json
import os
from pathlib import Path
from typing import List, Optional

import typer

from navig_sdk import console as ch
from navig_audio.commands.audio import audio_app


@audio_app.command("loudness")
def audio_loudness(
    files: List[Path] = typer.Argument(..., exists=True, dir_okay=False, readable=True,
                                       help="Audio files to measure."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable result."),
):
    """📏 Integrated loudness (LUFS), loudness range and true peak — how a master is judged."""
    from navig_audio.mastering.loudness import LoudnessError, measure

    rows = []
    for f in files:
        try:
            rows.append(measure(f))
        except LoudnessError as exc:
            ch.error(f"Could not measure {f.name}", str(exc))
            raise typer.Exit(1) from exc
    if as_json:
        ch.raw_print(_json.dumps([r.as_dict() for r in rows], indent=2, ensure_ascii=False))
        return
    for r in rows:
        ch.info(f"{r.lufs:7.2f} LUFS · LRA {r.lra:4.1f} LU · TP {r.true_peak:6.2f} dBTP   {Path(r.path).name}")


def _typed(p: Path | None) -> Path | None:
    """A path the operator typed, anchored to the directory they typed it in.

    Inside navig, ``main.py`` chdir's into the active space before the command runs and
    stashes the real directory in ``NAVIG_INVOCATION_CWD`` — so click's own ``exists=True``
    validated a relative ``--beat`` against the SPACE and refused a file that was right
    where the operator stood (measured: "File … does not exist" with the file present).
    Absolute paths pass through; standalone (no navig) falls back to the process cwd.
    """
    if p is None:
        return None
    p = Path(p).expanduser()
    if p.is_absolute():
        return p
    return Path(os.environ.get("NAVIG_INVOCATION_CWD") or Path.cwd()) / p


def _require_file(label: str, p: Path | None) -> None:
    """The existence check that used to live in the typer signature (see _typed)."""
    if p is None:
        return
    if not p.is_file():
        ch.error(f"{label} not found: {p}",
                 "A relative path resolves against the directory you typed it in.")
        raise typer.Exit(2)


@audio_app.command("master")
def audio_master(
    vocal: Path = typer.Argument(..., help="The voice track (dry take)."),
    beat: Path = typer.Option(..., "--beat", "-b",
                              help="The instrumental it was recorded on."),
    out: Optional[Path] = typer.Option(None, "--out", "-o",
                                       help="Output mp3 (default: <vocal>.master.mp3 beside the vocal; never overwrites)."),
    preset_name: str = typer.Option("ritual", "--preset", "-p", help="ritual | boom-bap | collage | clean."),
    lufs: Optional[float] = typer.Option(None, "--lufs", help="Loudness target (default: the preset's)."),
    tp: float = typer.Option(-1.0, "--tp", help="True-peak ceiling, dBTP."),
    offset: float = typer.Option(0.0, "--offset", help="Where the vocal starts in the beat, seconds."),
    vocal_gain: float = typer.Option(0.0, "--vocal-gain", help="Trim the vocal, dB, after the chain."),
    balance: Optional[float] = typer.Option(None, "--balance",
                                            help="How far the vocal sits above the beat, LU (default: the preset's)."),
    duck: Optional[float] = typer.Option(None, "--duck", help="Sidechain ratio for the beat under the voice (1 = off)."),
    denoise: bool = typer.Option(True, "--denoise/--no-denoise", help="Light hiss removal on the vocal."),
    vocal_ready: bool = typer.Option(False, "--vocal-ready",
                                     help="The vocal is already processed (compressed / EQ'd / effected in a DAW): "
                                          "only level and blend it, no second chain."),
    tone: str = typer.Option("none", "--voice-tone",
                             help="Colour the voice into a dark beat: none | dark | darker "
                                  "(roll the top off + a short room). Picked with --vocal-ready at "
                                  "--balance -5.5 (dark) / -7.5 (darker)."),
    wav: bool = typer.Option(False, "--wav", help="Also write a 24-bit WAV."),
    ref: Optional[Path] = typer.Option(None, "--ref",
                                       help="Master to match this reference track (needs: pip install navig-audio[master])."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the chain and input loudness; write nothing."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable result."),
):
    """🎚️ Mix a vocal over a beat and master it — a finished song at a measured loudness.

    The vocal is levelled, cleaned (low cut, light denoise), de-boxed, compressed, de-essed,
    given presence, air and the preset's space. The beat makes room at 2-4 kHz, sits a set
    number of LU under the *measured* vocal and ducks while the voice speaks. The mix is glued,
    then pushed to the loudness target with a limiter — measured, not assumed — and written as
    mp3 320k with a sidecar JSON of every number.
    """
    from navig_audio.mastering.chain import PRESETS, build_mix_graph, preset, voice_tone
    from navig_audio.mastering.loudness import LoudnessError, measure
    from navig_audio.mastering.master import MasterError, master, unique_path

    vocal, beat, ref = _typed(vocal), _typed(beat), _typed(ref)
    _require_file("vocal", vocal)
    _require_file("beat", beat)
    _require_file("reference", ref)
    try:
        p = preset(preset_name)
        voice_tone(tone)
    except ValueError as exc:
        ch.error(str(exc))
        raise typer.Exit(2) from exc
    if ref is not None:
        try:
            import matchering  # noqa: F401,PLC0415
        except ImportError as exc:
            ch.error("--ref needs matchering", "pip install 'navig-audio[master]'  (or: pip install matchering)")
            raise typer.Exit(2) from exc
    target = out or vocal.with_name(f"{vocal.stem}.master.mp3")
    target = unique_path(target)

    if dry_run:
        try:
            v, b = measure(vocal), measure(beat)
        except LoudnessError as exc:
            ch.error("Could not measure the inputs", str(exc))
            raise typer.Exit(1) from exc
        graph = build_mix_graph(p, offset_s=offset, vocal_gain_db=vocal_gain, duck_ratio=duck, denoise=denoise,
                                ready=vocal_ready, tone=tone)
        if as_json:
            ch.raw_print(_json.dumps({"preset": p.name, "target_lufs": lufs if lufs is not None else p.lufs,
                                      "vocal": v.as_dict(), "beat": b.as_dict(), "out": str(target),
                                      "graph": graph}, indent=2, ensure_ascii=False))
            return
        ch.info(f"preset {p.name} — {p.about}")
        ch.info(f"vocal {v.lufs:.1f} LUFS · beat {b.lufs:.1f} LUFS → target {lufs if lufs is not None else p.lufs} LUFS")
        ch.dim(graph)
        ch.dim(f"would write {target}  (presets: {', '.join(PRESETS)})")
        return

    try:
        with ch.create_spinner(f"Mastering ({p.name}, {lufs if lufs is not None else p.lufs} LUFS)…"):
            res = master(vocal, beat, target, p, lufs=lufs, tp=tp, offset_s=offset, vocal_gain_db=vocal_gain,
                         duck_ratio=duck, denoise=denoise, wav=wav, balance_lu=balance, reference=ref,
                         ready=vocal_ready, tone=tone)
    except (MasterError, LoudnessError) as exc:
        ch.error("Could not master it", str(exc))
        raise typer.Exit(1) from exc
    if as_json:
        ch.raw_print(_json.dumps(res.as_dict(), indent=2, ensure_ascii=False))
        return
    ch.success(f"Mastered → {res.out}",
               f"{res.lufs:.2f} LUFS (target {res.target_lufs}) · TP {res.true_peak:.2f} dBTP · "
               f"LRA {res.lra:.1f} LU · {res.duration_s:.1f} s · preset {res.preset}")
    if res.wav:
        ch.dim(f"wav: {res.wav}")
    ch.dim(f"every number: {res.sidecar}")
