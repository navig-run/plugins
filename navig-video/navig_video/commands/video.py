"""``navig video`` — render a channel's video formats from its workspace.

  navig video status                # workspace, engines, render verbs — one glance
  navig video doctor                # deeper checks; exit 1 when a required piece is missing

The render verbs (kinetic, cut, bulletin, format, rollcall, montage, variant) land as
the engines are ported; `status` lists which are wired. Everything reads a WORKSPACE
(``--workspace/-C``, env ``NAVIG_WORKSPACE``): media stays where it is, only the
rendering code lives in this plugin. `cut` is the exception that proves the rule —
its EDL carries its own paths.
"""

from __future__ import annotations

import json as _json
from pathlib import Path
from typing import Optional

import typer
from navig_sdk import console as ch
from navig_sdk.host import command_name

from navig_video.workspace import default_workspace, ffmpeg, ffprobe

CMD = command_name("video", standalone="navig-video")

#: Render verbs this plugin declares. `wired` flips as engines are ported (P3..P5 of
#: the channel migration); status/doctor report the truth, never an aspiration.
#: (verb, one-line what it will do). `wired` is COMPUTED from the mounted
#: command tree — a verb cannot claim to exist, and cannot be forgotten here
#: when it lands (see _wired).
RENDER_VERBS = (
    ("kinetic render", "Word-timed kinetic episode (voice drives the cuts)."),
    ("kinetic plan", "Dump an episode's timeline without rendering."),
    ("cut", "Render an EDL (the editable timeline) into its outputs."),
    ("bulletin", "Narrated TRANSMISSION page."),
    ("format", "One-picture formats (face/wall/door/board/plant)."),
    ("rollcall", "Scrolling zone-list video."),
    ("montage", "Cut from existing footage, matched by grade."),
    ("variant", "Per-destination encode variant."),
)


def _wired(verb: str) -> bool:
    """True when the verb path resolves on this app (group by group).

    Walks the Typer registrations directly — plugins never import click, and
    ``registered_groups`` holds ``TyperInfo`` wrappers (``typer_instance`` is
    the Typer underneath).
    """
    words = verb.split()
    if not words:
        return False
    cmds = {c.name for c in video_app.registered_commands}
    groups = {g.name: getattr(g, "typer_instance", None) for g in video_app.registered_groups}
    if words[0] in cmds:
        return len(words) == 1
    node = groups.get(words[0])
    if node is None:
        return False
    for i, word in enumerate(words[1:], start=1):
        last = i == len(words) - 1
        sub_cmds = {c.name for c in node.registered_commands}
        if word in sub_cmds:
            return last
        sub_groups = {g.name: getattr(g, "typer_instance", None) for g in node.registered_groups}
        node = sub_groups.get(word)
        if node is None:
            return False
    return True


video_app = typer.Typer(
    name="video",
    help="🎬 Video — render a channel's kinetic episodes, bulletins and formats from its workspace.",
    no_args_is_help=True,
)


def _engines() -> dict[str, bool]:
    """Which sibling engines import right now (lazy — a missing one is a report, not a crash).

    ``navig_media_core`` owns the frame/mix/encode primitives (extracted from
    navig-generate, PR #1908); ``navig_generate`` keeps the generator clients a
    plate render needs; ``navig_audio`` is voice and beats.
    """
    out: dict[str, bool] = {}
    for mod in ("navig_audio", "navig_media_core", "navig_generate"):
        try:
            __import__(mod)
            out[mod] = True
        except ImportError:
            out[mod] = False
    return out


@video_app.command("status")
def video_status(
    workspace: Optional[Path] = typer.Option(
        None, "--workspace", "-C", help="Channel workspace directory."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit status as JSON."),
) -> None:
    """Workspace, engines and render verbs — one glance at what is wired."""
    ws = default_workspace(workspace)
    engines = _engines()
    rows = {
        "workspace": str(ws),
        "workspaceExists": ws.is_dir(),
        "ffmpeg": ffmpeg() or None,
        "ffprobe": ffprobe() or None,
        "engines": engines,
        "verbs": [
            {"verb": v, "wired": _wired(v), "about": about}
            for v, about in RENDER_VERBS
        ],
    }
    if as_json:
        ch.raw_print(_json.dumps(rows, indent=2))
        return

    ch.subheader("navig video · workspace")
    mark = "●" if rows["workspaceExists"] else "○"
    ch.raw_print(f" {mark} {ws}")
    ch.raw_print(f" {'●' if rows['ffmpeg'] else '○'} ffmpeg: {rows['ffmpeg'] or 'not on PATH'}")
    for mod, ok in engines.items():
        ch.raw_print(f" {'●' if ok else '○'} {mod}: {'importable' if ok else 'not installed'}")
    ch.subheader("render verbs")
    for v, about in RENDER_VERBS:
        state = "[green]wired[/green]" if _wired(v) else "[dim]planned[/dim]"
        ch.raw_print(f" {state:18} {CMD} {v} — {about}")
    if not rows["workspaceExists"]:
        ch.warning("Workspace missing", "pass --workspace <dir> or set NAVIG_WORKSPACE")


@video_app.command("doctor")
def video_doctor(
    workspace: Optional[Path] = typer.Option(
        None, "--workspace", "-C", help="Channel workspace directory."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit the report as JSON."),
) -> None:
    """Deep check: workspace layout, engines, ffmpeg, fonts. Exit 1 if a requirement fails."""
    ws = default_workspace(workspace)
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str, required: bool = True) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail, "required": required})

    add("workspace", ws.is_dir(), str(ws))
    if ws.is_dir():
        add("master texts", (ws / "out").is_dir(), f"{ws / 'out'}")
        add("episodes.py", (ws / "episodes.py").is_file(), "channel episode data")
        add("plates", (ws / "plates").is_dir(), f"{ws / 'plates'}", required=False)
        add("voice cache", (ws / "vo").is_dir(), f"{ws / 'vo'}", required=False)
    add("ffmpeg", bool(ffmpeg()), ffmpeg() or "not on PATH")
    add("ffprobe", bool(ffprobe()), ffprobe() or "not on PATH", required=False)
    for mod, ok in _engines().items():
        add(mod, ok, "importable" if ok else "pip install " + mod.replace("_", "-"), required=False)

    failed = [c for c in checks if c["required"] and not c["ok"]]
    if as_json:
        ch.raw_print(_json.dumps({"ok": not failed, "checks": checks}, indent=2))
    else:
        for c in checks:
            glyph = "✓" if c["ok"] else ("✗" if c["required"] else "·")
            req = "" if c["required"] else " (optional)"
            ch.raw_print(f" {glyph} {c['check']}: {c['detail']}{req}")
        if failed:
            ch.error(f"{len(failed)} required check(s) failed", "fix the ✗ lines above")
        else:
            ch.success("Ready to render", f"{CMD} status lists the verbs")
    if failed:
        raise typer.Exit(1)

# `navig video kinetic …` — mounted by import side-effect, like audio's beat group.
from navig_video.commands.kinetic import kinetic_app  # noqa: E402

video_app.add_typer(kinetic_app, name="kinetic")

# `navig video parity …` — mounted by import side-effect, like kinetic.
from navig_video.commands.parity import video_parity  # noqa: E402,F401

# `navig video cut …` — the EDL renderer (plan 21), mounted the same way.
from navig_video.commands.cut import video_cut  # noqa: E402,F401
