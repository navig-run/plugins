"""``navig audio regenerate`` — new audio made from an existing recording, by AI.

  navig audio regenerate take.wav --op voice --voice <voice-id> --allow-cloud
  navig audio regenerate money.mp3 --op variations --direction "heavier, 8-bit arcade" -n 3 --allow-cloud
  navig audio regenerate song.mp3 --op replace --stem drums --with "lo-fi brushed kit" --allow-cloud
  navig audio regenerate song.mp3 --op replace --stem vocals --voice <voice-id> --allow-cloud
  navig audio regenerate money.mp3 --op variations --dry-run     what would be sent and billed

Everything this makes is GENERATED — a new take, labelled as such, never a restoration
(that is ``navig audio enhance``). The original is never written. Every run that uploads
or bills needs ``--allow-cloud``; ``--describe-with-ai`` also uploads the clip to an
audio-capable model to name what it hears. Mounted on ``audio_app`` from ``audio.py``.
"""

from __future__ import annotations

import json as _json
from pathlib import Path
from typing import Optional

import typer

from navig_audio._typed_path import resolve_user_path
from navig_audio.commands.audio import audio_app
from navig_sdk import console as ch


@audio_app.command("regenerate")
def audio_regenerate(
    file: Path = typer.Argument(..., help="The recording to make new audio from (left untouched)."),
    op: str = typer.Option(..., "--op", help="voice · variations · replace"),
    voice: Optional[str] = typer.Option(None, "--voice", help="ElevenLabs voice id (voice, or replace --stem vocals)."),
    direction: str = typer.Option("", "--direction", "-d", help="variations: how the new takes should differ."),
    count: int = typer.Option(2, "--count", "-n", min=1, max=4, help="variations: how many takes (1–4)."),
    stem: Optional[str] = typer.Option(None, "--stem", help="replace: drums · bass · guitar · piano · vocals · other"),
    replace_with: str = typer.Option("", "--with", help="replace: what the new stem should be."),
    describe_ai: bool = typer.Option(False, "--describe-with-ai", help="Let an AI listen and describe the clip (uploads it)."),
    category: str = typer.Option("auto", "--category", help="auto · speech · sfx · music · ambience · mixed"),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Output folder (default: next to the original)."),
    allow_cloud: bool = typer.Option(False, "--allow-cloud", help="Allow the upload / billing this run needs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be sent and billed; generate nothing."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable result."),
) -> None:
    """🧪 Make NEW audio from a recording with AI — new voice, variations, or a replaced stem."""
    from navig_audio.regenerate import RegenerateError, regenerate

    src = resolve_user_path(file)
    if not src.is_file():
        ch.error(f"No such file: {src}", "A relative path resolves against the directory you typed it in.")
        raise typer.Exit(2)

    def _say(msg: str, _f: float) -> None:
        if not as_json:
            ch.dim(f"… {msg}")

    try:
        res = regenerate(
            src, op=op, allow_cloud=allow_cloud, out_dir=resolve_user_path(out) if out else None,
            voice_id=voice, direction=direction, count=count, stem=stem, replace_with=replace_with,
            use_ai_description=describe_ai, category=category, dry_run=dry_run, progress=_say,
        )
    except RegenerateError as exc:
        ch.error("Regenerate refused" if "allow cloud" in str(exc) else "Regenerate failed", str(exc))
        if "allow cloud" in str(exc):
            ch.info("Re-run with --allow-cloud once you are fine with the upload/billing (see --dry-run first).")
        raise typer.Exit(2 if "allow cloud" in str(exc) else 1) from None

    if as_json:
        ch.raw_print(_json.dumps(res.to_dict(), indent=2, ensure_ascii=False))
        return
    plan = res.plan
    ch.console.print("[magenta]● GENERATED audio[/magenta] — new takes made from the original, not a restoration.")
    if res.description:
        ch.dim(f"description: {res.description.text}")
        if res.description.heard_by:
            ch.dim(f"(heard by {res.description.heard_by} — an AI can mishear)")
    sends = []
    if plan.get("uploads_source"):
        sends.append("uploads the recording to ElevenLabs")
    if plan.get("uploads_to_ai"):
        sends.append("uploads it to an AI to describe")
    if not sends:
        sends.append("sends only a text prompt to ElevenLabs")
    ch.dim(f"{plan['op']}: {'; '.join(sends)}; ~{plan.get('seconds_billed', 0)} s of generated audio billed by ElevenLabs")
    if dry_run:
        ch.info("Dry run — nothing generated. Add --allow-cloud (and drop --dry-run) to make it.")
        return
    table = ch.create_table(columns=[
        {"name": "take", "no_wrap": True},
        {"name": "closeness to the original"},
    ])
    for t in res.takes:
        s = t.similarity
        table.add_row(Path(t.path).name, f"{s.get('reading', '')} · envelope {s.get('envelope')} · tone {s.get('tone')} · pitch {s.get('pitch')}")
    ch.print_table(table)
    for t in res.takes:
        ch.success(f"Saved {t.path}")
    ch.dim(f"Provenance: {res.sidecar}")
