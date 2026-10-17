"""``navig audio voice-line`` — one timed narration line, cached by content.

  navig audio voice-line "You know your neighbour's Wi-Fi." --lang en
  navig audio voice-line "…" --out line.mp3 --timings line.json --voice brian
  navig audio voice-line "…" --semitones -3.5     # re-pitches the cache, no API call
  navig audio voice-stats                        # what the shared cache holds

Mounted on ``audio_app`` by import side-effect from ``audio.py``, like ``beat``.
"""

from __future__ import annotations

import json as _json
import shutil
from pathlib import Path
from typing import Optional

import typer

from navig_sdk import console as ch
from navig_audio._typed_path import resolve_user_path
from navig_audio.commands.audio import audio_app

CMD = "navig audio voice-line"


@audio_app.command("voice-line")
def audio_voice_line(
    text: str = typer.Argument(..., help="The line to speak (the display text; wrap red words in *asterisks* as usual)."),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Copy the mp3 here (default: keep it in the voice cache)."),
    voice: str = typer.Option("brian", "--voice", help="Roster name (brian, charlie, …) or a raw ElevenLabs voice id."),
    lang: str = typer.Option("en", "--lang", help="Language tag — picks the voice settings and the deepen (en/ru/fr)."),
    semitones: Optional[float] = typer.Option(None, "--semitones", help="Override the per-language pitch drop; re-pitches from the stored raw, never re-bills."),
    timings: Optional[Path] = typer.Option(None, "--timings", help="Write the word-timings JSON here."),
    cache_dir: Optional[Path] = typer.Option(None, "--cache-dir", help="Voice cache directory (default: <workspace>/vo/kinetic)."),
    force: bool = typer.Option(False, "--force", help="Ignore the cache and synthesize again (bills the voice API)."),
    workspace: Optional[Path] = typer.Option(None, "--workspace", "-C", help="Channel workspace (default: NAVIG_WORKSPACE or the channel directory)."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable result."),
) -> None:
    """🗣 One narration line with word timings — the kinetic renderer reads the same cache."""
    from navig_audio.voice.line import LineError, duration, synth_timed
    from navig_audio.workspace import default_workspace

    if out is not None:
        out = resolve_user_path(out)
    if timings is not None:
        timings = resolve_user_path(timings)
    cache = resolve_user_path(cache_dir) if cache_dir else default_workspace(workspace) / "vo" / "kinetic"
    try:
        result = synth_timed(text, voice, lang, cache, force=force, semitones=semitones)
    except LineError as exc:
        ch.error("Voice line failed", str(exc))
        raise typer.Exit(1) from exc

    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.resolve() != result.path.resolve():
            shutil.copyfile(result.path, out)
    if timings is not None:
        timings.parent.mkdir(parents=True, exist_ok=True)
        timings.write_text(
            _json.dumps(result.words, ensure_ascii=False), encoding="utf-8"
        )

    secs = duration(result.path)
    if as_json:
        ch.raw_print(_json.dumps(
            {
                "path": str(out or result.path),
                "key": result.key,
                "cached": result.cached,
                "seconds": round(secs, 3),
                "words": len(result.words),
                "timings": str(timings) if timings else str(result.timings_path),
            },
            indent=2, ensure_ascii=False,
        ))
        return
    src = "cached" if result.cached else "synthesized"
    ch.success(f"Voice line {src}", f"{secs:.1f}s · {len(result.words)} words · key {result.key}")
    ch.raw_print(str(out or result.path))
    ch.dim(f"timings: {timings or result.timings_path}")


@audio_app.command("voice-stats")
def audio_voice_stats(
    workspace: Optional[Path] = typer.Option(None, "--workspace", "-C", help="Channel workspace (default: NAVIG_WORKSPACE or the channel directory)."),
    cache_dir: Optional[Path] = typer.Option(None, "--cache-dir", help="Voice cache directory (default: <workspace>/vo/kinetic)."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable result."),
) -> None:
    """📊 What the shared voice cache holds — counts, size, orphans, age.

    Read-only: every usable pair here is a line that never re-bills, and every
    orphan is one the renderer cannot serve — it synthesizes (and bills) again.
    """
    from navig_audio.voice.line import cache_stats
    from navig_audio.workspace import default_workspace

    cache = resolve_user_path(cache_dir) if cache_dir else default_workspace(workspace) / "vo" / "kinetic"
    try:
        data = cache_stats(cache)
    except OSError as exc:
        ch.error("Cache unreadable", str(exc))
        raise typer.Exit(1) from exc
    if as_json:
        ch.raw_print(_json.dumps(data, indent=2, ensure_ascii=False))
        return
    if not data["exists"]:
        ch.warning("No voice cache", f"{data['dir']} — nothing synthesized yet")
        return
    orphans = {k: v for k, v in data["orphans"].items() if v}
    n_orphans = sum(len(v) for v in orphans.values())
    ch.success(
        "Voice cache",
        f"{data['mp3']} mp3 · {data['bytes'] / 1_000_000:.1f} MB"
        + (f" · {n_orphans} orphan(s)" if n_orphans else " · no orphans"),
    )
    for kind, names in orphans.items():
        ch.warning(kind, ", ".join(names[:12]) + (" …" if len(names) > 12 else ""))
    if data["oldest"]:
        ch.dim(f"age: {data['oldest']} → {data['newest']}")
    if data["tmp"]:
        ch.dim(f"{data['tmp']} leftover .tmp.mp3 (a write died mid-file)")
    ch.raw_print(data["dir"])
