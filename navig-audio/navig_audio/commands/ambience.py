"""``navig audio ambience`` — generated room weather, license-free.

  navig audio ambience rain --seconds 18.5      # <workspace>/sound/amb-rain-19.wav
  navig audio ambience city --seconds 30 --out ./

Mounted on ``audio_app`` by import side-effect from ``audio.py``, like ``beat``.
"""

from __future__ import annotations

import json as _json
from pathlib import Path
from typing import Optional

import typer

from navig_sdk import console as ch
from navig_audio._typed_path import resolve_user_path
from navig_audio.commands.audio import audio_app


@audio_app.command("ambience")
def audio_ambience(
    kind: str = typer.Argument(..., help="rain | static | city"),
    seconds: float = typer.Option(..., "--seconds", "-s", min=0.1, help="Length of the weather, in seconds."),
    out_dir: Optional[Path] = typer.Option(None, "--out", "-o", help="Directory to write into (default: <workspace>/sound)."),
    force: bool = typer.Option(False, "--force", help="Regenerate even if the file exists."),
    workspace: Optional[Path] = typer.Option(None, "--workspace", "-C", help="Channel workspace."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable result."),
) -> None:
    """🌧 Rain, static or city weather under a cut — deterministic, seeded by kind."""
    from navig_audio.ambience import KINDS, amb_wav
    from navig_audio.workspace import default_workspace

    if kind not in KINDS:
        ch.error("Unknown ambience", f"{kind!r} is not one of: {', '.join(KINDS)}")
        raise typer.Exit(2)
    target = resolve_user_path(out_dir) if out_dir else default_workspace(workspace) / "sound"
    try:
        path = amb_wav(kind, seconds, target, force=force)
    except (OSError, ValueError) as exc:
        ch.error("Ambience failed", str(exc))
        raise typer.Exit(1) from exc
    if as_json:
        ch.raw_print(_json.dumps(
            {"path": str(path), "kind": kind, "seconds": seconds}, indent=2))
        return
    ch.success("Ambience ready", str(path))
