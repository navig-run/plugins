"""``navig audio bed`` — the channel's music: one track per episode, never shared.

  navig audio bed assign loading --workspace …        # (track, offset), stable
  navig audio bed assign loading --mood humor         # when the workspace has no episode data

Mounted on ``audio_app`` by import side-effect from ``audio.py``, like ``beat``.
"""

from __future__ import annotations

import json as _json
from pathlib import Path
from typing import Optional

import typer

from navig_sdk import console as ch
from navig_audio.commands.audio import audio_app

bed_app = typer.Typer(
    name="bed",
    help="🎵 The channel's music bed — one track per episode, never shared.",
    no_args_is_help=True,
)
audio_app.add_typer(bed_app, name="bed")


def _mood_for(name: str, workspace: Path, explicit: Optional[str]) -> str:
    """--mood wins; else the episode's declared mood in the workspace; else error."""
    if explicit:
        return explicit
    from navig_audio.beds import MOODS
    from navig_audio.workspace import load_workspace_module

    try:
        EP = load_workspace_module(workspace, "episodes")
        ep = EP.EPISODES.get(name)
    except (ImportError, AttributeError, OSError):
        ep = None
    if isinstance(ep, dict):
        from navig_audio.beds import mood_for

        return mood_for(name, ep)
    ch.error(
        "No mood for this episode",
        f"{name} is not in the workspace episodes and --mood was not given — "
        f"use one of: {', '.join(MOODS)}",
    )
    raise typer.Exit(2)


@bed_app.command("assign")
def bed_assign(
    name: str = typer.Argument(..., help="Episode id (the store keys assignments by it)."),
    mood: Optional[str] = typer.Option(None, "--mood", help="dark | mystery | humor | epic (default: the episode's own mood)."),
    workspace: Optional[Path] = typer.Option(None, "--workspace", "-C", help="Channel workspace."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable result."),
) -> None:
    """🎼 (Re)use the episode's track — stable across renders, unique per episode."""
    from navig_audio.beds import BedError, assign, store_path
    from navig_audio.workspace import default_workspace

    ws = default_workspace(workspace)
    resolved = _mood_for(name, ws, mood)
    try:
        track, at = assign(name, resolved, ws)
    except BedError as exc:
        ch.error("No bed assigned", str(exc))
        raise typer.Exit(1) from exc
    if as_json:
        ch.raw_print(_json.dumps(
            {"episode": name, "mood": resolved, "path": str(track), "at": at,
             "store": str(store_path(ws))},
            indent=2, ensure_ascii=False))
        return
    ch.success(f"Bed for {name}", f"{track.name} @ {at}s ({resolved})")
    ch.raw_print(str(track))
