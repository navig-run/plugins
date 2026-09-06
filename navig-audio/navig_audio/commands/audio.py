"""``navig audio`` — AI audio generation (music · SFX · text-to-speech).

The audio facet's user-facing verb. Voice (speak / transcribe / wake-word) stays
under ``navig voice``; this group owns *generation*:

  navig audio gen "lofi hip-hop, rainy night" --kind music --duration 30
  navig audio gen "a door creaking open"       --kind sfx
  navig audio gen "Welcome aboard."            --kind tts --voice <id>
  navig audio check                            is a provider key configured?

The provider client lives in core (``navig.tools.audio_generation``); this
command wraps it and, on ``--count``, writes each clip under a unique name.
No secret is ever printed — only whether a key resolves.
"""

from __future__ import annotations

import asyncio
import json as _json
from pathlib import Path
from typing import Optional

import typer

from navig.lazy_loader import lazy_import

ch = lazy_import("navig.console_helper")

audio_app = typer.Typer(
    name="audio",
    help="🎵 AI audio generation — music, sound effects, and text-to-speech.",
    no_args_is_help=True,
)

_KINDS = ("music", "sfx", "tts")


@audio_app.command("gen")
def audio_gen(
    prompt: str = typer.Argument(
        ..., help="What to generate (a song brief, an SFX description, or text to speak)."
    ),
    kind: str = typer.Option("music", "--kind", "-k", help="music | sfx | tts"),
    duration: Optional[float] = typer.Option(
        None, "--duration", "-d", help="Target duration in seconds (music / sfx)."
    ),
    voice: Optional[str] = typer.Option(
        None, "--voice", help="ElevenLabs voice id (tts only)."
    ),
    count: int = typer.Option(
        1, "--count", "-n", min=1, max=10, help="How many clips to generate."
    ),
    out: Optional[Path] = typer.Option(
        None, "--out", "-o", help="Directory to save into (default: ~/.navig/audio)."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit the result as JSON."),
) -> None:
    """Generate audio from a text prompt and save it locally."""
    kind_l = kind.lower().strip()
    if kind_l not in _KINDS:
        ch.error(f"Unknown kind {kind!r}", f"Use one of: {', '.join(_KINDS)}")
        raise typer.Exit(2)

    try:
        from navig.tools.audio_generation import (
            AudioGenerationConfig,
            AudioGenerator,
            is_audio_generation_available,
        )
    except ImportError as exc:  # httpx missing, or a very old core
        ch.error("Audio generation unavailable", str(exc))
        raise typer.Exit(2) from exc

    if not is_audio_generation_available():
        ch.error(
            "No ElevenLabs API key configured",
            "Add one:  navig vault set elevenlabs <key>   (or set ELEVENLABS_API_KEY)",
        )
        raise typer.Exit(2)

    cfg = AudioGenerationConfig.from_env()
    if out is not None:
        cfg.output_dir = str(out)

    async def _run() -> list:
        gen = AudioGenerator(cfg)
        results: list = []
        try:
            for i in range(count):
                aud = await gen.generate(
                    prompt, kind=kind_l, duration_s=duration, voice_id=voice
                )
                if count > 1 and aud.local_path:
                    src = Path(aud.local_path)
                    if src.exists():
                        dst = src.with_name(f"{src.stem}_{i:02d}{src.suffix}")
                        src.replace(dst)
                        aud.local_path = str(dst)
                results.append(aud)
        finally:
            await gen.close()
        return results

    with ch.create_spinner(f"Generating {kind_l}…"):
        try:
            results = asyncio.run(_run())
        except Exception as exc:  # network / provider / quota — surface it, don't swallow
            ch.error("Audio generation failed", str(exc))
            raise typer.Exit(1) from exc

    # A result only counts as generated if it actually wrote a file: the provider can
    # return a result object with no/dead path (quota, a rejected prompt, a bad voice
    # id), and reporting "Generated N clip(s)" over those is a phantom success — the
    # user is told audio was saved when nothing was.
    def _wrote(r) -> bool:
        return bool(r.local_path and Path(r.local_path).exists())

    ok = [r for r in results if _wrote(r)]

    if as_json:
        ch.raw_print(_json.dumps([r.to_dict() for r in results], indent=2))
        raise typer.Exit(0 if ok else 1)

    table = ch.create_table(
        columns=[
            {"name": "#", "style": "dim", "justify": "right"},
            {"name": "kind", "style": "cyan"},
            {"name": "model", "style": "magenta"},
            {"name": "file"},  # free-text column — wraps on a narrow terminal
        ],
    )
    for idx, r in enumerate(results, 1):
        table.add_row(str(idx), r.kind.value, r.model or "—",
                      r.local_path if _wrote(r) else "[red]— failed[/red]")
    ch.print_table(table)

    if not ok:
        ch.error(
            f"No {kind_l} clip was generated",
            f"The provider returned {len(results)} result(s) but wrote no audio file "
            f"(check your quota, prompt, or voice id).",
        )
        raise typer.Exit(1)
    failed = len(results) - len(ok)
    ch.success(
        f"Generated {len(ok)} {kind_l} clip(s)" + (f"  ({failed} failed)" if failed else ""),
        f"Saved under {cfg.output_dir}",
    )


@audio_app.command("check")
def audio_check(
    as_json: bool = typer.Option(False, "--json", help="Emit the status as JSON."),
) -> None:
    """Show whether AI audio generation is configured (a provider key resolves)."""
    from navig.tools.media_providers import MEDIA_CATALOG, key_status

    rows = []
    for entry in MEDIA_CATALOG.get("audio", []):
        rows.append(
            {
                "provider": entry["label"],
                "id": entry["id"],
                "kinds": ", ".join(m["id"] for m in entry.get("models", [])),
                "configured": key_status(entry),
                "get_key": entry.get("get_key", ""),
            }
        )

    if as_json:
        ch.raw_print(_json.dumps(rows, indent=2))
        return

    table = ch.create_table(
        columns=[
            {"name": "provider", "style": "cyan"},
            {"name": "kinds", "style": "dim"},
            {"name": "status"},
            {"name": "next step"},  # free-text column
        ],
    )
    any_ready = False
    for r in rows:
        if r["configured"]:
            any_ready = True
            status = "[green]● ready[/green]"
            nxt = "→ navig audio gen \"…\""
        else:
            status = "[dim]○ not configured[/dim]"
            nxt = f"→ navig vault set {r['id']} <key>"
        table.add_row(r["provider"], r["kinds"], status, nxt)
    ch.print_table(table)

    if any_ready:
        ch.dim("Audio generation ready · navig audio gen \"<prompt>\" --kind music|sfx|tts")
    else:
        ch.warning(
            "No audio provider configured",
            "Get a free ElevenLabs key, then:  navig vault set elevenlabs <key>",
        )


# ── editing ────────────────────────────────────────────────────────────────────
#
# "Play it slower" is two different requests and users mean both: keep the voices
# where they are (tempo), or let the pitch fall with the speed (the "slowed" sound).
# ffmpeg does them with different filters, so they are different verbs here.
#
# Transcription is deliberately NOT re-implemented in this group — it already exists
# as `navig voice transcribe <file>` (and `navig agent transcribe`). A third copy
# would be a parallel system, not a feature.


def _default_out(src: Path, suffix: str) -> Path:
    return src.with_name(f"{src.stem}-{suffix}{src.suffix}")


def _emit_edit(result, as_json: bool, what: str) -> None:
    from navig.media.audio_edit import EditResult  # noqa: F401  (typing only, lazy)

    if as_json:
        ch.raw_print(_json.dumps({
            "path": str(result.path),
            "rate": result.rate,
            "pitch_shifted": result.pitch_shifted,
            "filters": result.filters,
        }, indent=2))
        return
    size = result.path.stat().st_size if result.path.exists() else 0
    ch.success(f"{what} → {result.path}", f"{size / 1024:.0f} KB · filter: {result.filters}")


@audio_app.command("speed")
def audio_speed(
    file: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True,
                                help="Audio file to re-time (mp3, wav, m4a, ogg, …)."),
    rate: float = typer.Option(..., "--rate", "-r",
                               help="Speed multiplier: 1.5 = faster, 0.75 = slower."),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Output file (default: <name>-<rate>x)."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable result."),
):
    """⏩ Change speed, keeping pitch — a lecture at 1.5x still sounds like the speaker.

    Examples:
        navig audio speed talk.mp3 --rate 1.5
        navig audio speed note.ogg --rate 0.8 -o slower.ogg
    """
    from navig.media.audio_edit import AudioEditError, speed

    if rate <= 0:
        ch.error("--rate must be greater than 0")
        raise typer.Exit(2)
    target = out or _default_out(file, f"{rate:g}x")
    try:
        result = speed(file, target, rate)
    except AudioEditError as exc:
        ch.error("Could not change the speed", str(exc))
        raise typer.Exit(1) from exc
    _emit_edit(result, as_json, f"{rate:g}x (pitch kept)")


@audio_app.command("slowed")
def audio_slowed(
    file: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True,
                                help="Audio file to slow down."),
    rate: float = typer.Option(0.85, "--rate", "-r", help="How slow (0.85 is the usual 'slowed' feel)."),
    reverb: bool = typer.Option(True, "--reverb/--no-reverb", help="Add a little space around it."),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Output file (default: <name>-slowed)."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable result."),
):
    """🐢 The "slowed" sound — pitch drops with the speed, plus optional reverb.

    This is the tape-machine effect, NOT `speed --rate 0.85`: there the voices stay put,
    here they drop with it.

        navig audio slowed track.mp3
        navig audio slowed track.mp3 --rate 0.8 --no-reverb
    """
    from navig.media.audio_edit import AudioEditError, slowed

    if rate <= 0:
        ch.error("--rate must be greater than 0")
        raise typer.Exit(2)
    target = out or _default_out(file, "slowed")
    try:
        result = slowed(file, target, rate, reverb=reverb)
    except AudioEditError as exc:
        ch.error("Could not slow it down", str(exc))
        raise typer.Exit(1) from exc
    _emit_edit(result, as_json, f"slowed to {rate:g}x{' + reverb' if reverb else ''}")


# ── episode verbs ─────────────────────────────────────────────────────────────
#
# `episode.py` attaches draft / plan / translate / render / voices / clone / publish to
# `audio_app` as an import side effect, so THIS LINE is the only thing that mounts them.
# Without it they are dead code no CLI surface can reach — and because nothing fails,
# the verbs simply do not appear in `--help` and the omission looks like a design choice.
# It sits at the bottom because `episode` imports `audio_app` back from this module; by
# here it exists, so the cycle resolves.
from navig_audio.commands import episode as _episode  # noqa: E402,F401
