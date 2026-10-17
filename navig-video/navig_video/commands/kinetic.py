"""``navig video kinetic`` — a monologue cut word by word, the way it is said.

  navig video kinetic render neighbour                    # English
  navig video kinetic render soul --lang ru               # Russian
  navig video kinetic render all --lang en,ru             # every episode, both languages
  navig video kinetic render neighbour --still 5.2        # one frame, for checking
  navig video kinetic plan neighbour --json               # the timeline, no render

Timing comes from the voice itself (ElevenLabs word timestamps); the cut grid
comes from the bed; the voice cache is the workspace's own, so a re-render
never re-bills. Live numbers ({citiesOpen}, {daysLeft}, …) are read from the
channel's board at build time — a fetch failure falls back to the disk cache
and says so.

Mounted on ``video_app`` by import side-effect from ``video.py``.
"""

from __future__ import annotations

import json as _json
import urllib.request
from pathlib import Path
from typing import Optional

import typer

from navig_sdk import console as ch
from navig_video._typed_path import resolve_user_path

kinetic_app = typer.Typer(
    name="kinetic",
    help="⚡ Kinetic episodes — word-by-word slams driven by the voice's own timing.",
    no_args_is_help=True,
)

CMD = "navig video kinetic"


def _vals(ws: Path, names: list[str], langs: list[str]) -> tuple[dict, dict | None]:
    """Live numbers for the cards — workspace-shaped, cached, loud on failure.

    Returns (vals, vals_stp): Saint Petersburg's seat count rides the RU
    loading episode (owner rule), falling back to the board's own lead.
    """
    from navig_video.workspace import load_workspace_module

    B = load_workspace_module(ws, "build")
    L = load_workspace_module(ws, "live")
    try:
        raw = L.fetch()
    except Exception as exc:  # SystemExit from live.fetch, or a shape error
        ch.error("Live numbers unavailable", str(exc))
        raise typer.Exit(1) from exc
    vals = B.flat(raw)
    ru = {"Montpellier": "Монпелье", "Saint Petersburg": "Санкт-Петербург"}
    vals["lead_name_ru"] = ru.get(vals.get("lead_name"), vals.get("lead_name"))
    vals_stp = None
    if "loading" in names:
        try:
            req = urllib.request.Request(
                "https://api.schema.cx/api/landrun",
                headers={"User-Agent": "schema-kinetic/1.0"})
            zones = _json.load(urllib.request.urlopen(req, timeout=20)).get("zones") or []  # noqa: S310
            stp = next((z for z in zones if z["name"] == "Saint Petersburg"), None)
            if stp:
                vals_stp = dict(vals, lead_toMayor=stp.get("toMayor") or 0)
        except Exception as exc:                     # fall back to the board's lead
            ch.warning("Saint Petersburg lookup failed", str(exc))
    return vals, vals_stp


def _languages(langs: str) -> list[str]:
    return [x.strip() for x in langs.split(",") if x.strip()]


def _engines_or_exit() -> None:
    """The audio engines are an extra — say so cleanly instead of a traceback."""
    missing = []
    for mod in ("navig_audio.voice.line", "navig_audio.speech",
                "navig_audio.beds", "navig_audio.bedgrid"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod.split(".")[1])
    if missing:
        ch.error(
            "Kinetic needs the audio engines",
            "missing: " + ", ".join(sorted(set(missing)))
            + ' — install them with: pip install "navig-video[render]"',
        )
        raise typer.Exit(1)


def _fonts_or_exit() -> None:
    """Refuse before synth/render when a slam or mono face is absent.

    The frame engine has no font fallback, so the only honest outcomes are
    this early named exit or a traceback mid-frames — this is the early one.
    """
    from navig_video.kinetic import pictures  # noqa: PLC0415

    missing = pictures.missing_fonts()
    if missing:
        ch.error(
            "Font missing",
            f"{', '.join(missing)} — kinetic renders with no fallback; install "
            "to %LOCALAPPDATA%\\Microsoft\\Windows\\Fonts",
        )
        raise typer.Exit(1)


@kinetic_app.command("render")
def kinetic_render(
    episode: str = typer.Argument(..., help="Episode id, comma-list, or 'all'."),
    lang: str = typer.Option("en", "--lang", help="Comma-list: en,ru,fr."),
    still: Optional[float] = typer.Option(None, "--still", help="Write one frame at this second instead of rendering."),
    stills: bool = typer.Option(False, "--stills", help="Force still plates even where a motion clip exists."),
    out: Optional[Path] = typer.Option(None, "--out", help="Output root (default: <workspace>/out/kinetic)."),
    workspace: Optional[Path] = typer.Option(None, "--workspace", "-C", help="Channel workspace."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable result."),
) -> None:
    """🎬 Render kinetic episodes — voice timing drives every cut."""
    from navig_video.kinetic.ctx import bind
    from navig_video.workspace import default_workspace, load_workspace_module

    _engines_or_exit()
    ws = default_workspace(workspace)
    if not ws.is_dir():
        ch.error("Workspace not found", f"{ws} — pass --workspace <dir> or set NAVIG_WORKSPACE")
        raise typer.Exit(2)
    _fonts_or_exit()
    bind(ws, resolve_user_path(out) if out else None)  # binds the renderer's paths for this command
    EP = load_workspace_module(ws, "episodes")
    names = list(EP.EPISODES) if episode == "all" else [n.strip() for n in episode.split(",") if n.strip()]
    unknown = [n for n in names if n not in EP.EPISODES]
    if unknown:
        ch.error("Unknown episode(s)", f"{', '.join(unknown)} — workspace has: {', '.join(list(EP.EPISODES)[:8])} …")
        raise typer.Exit(2)
    langs = _languages(lang)

    vals, vals_stp = _vals(ws, names, langs)
    rendered: list[dict] = []
    for n in names:
        for lg in langs:
            if lg not in EP.EPISODES[n]["beats"][0]["say"]:
                continue
            v = vals_stp if (n == "loading" and lg == "ru" and vals_stp) else vals
            try:
                if still is not None:
                    from navig_video.kinetic.render import still as render_still

                    p = render_still(n, lg, v, still)
                else:
                    from navig_video.kinetic.render import render

                    p = render(n, lg, v, force_stills=stills)
                rendered.append({"episode": n, "lang": lg, "path": str(p)})
            except FileNotFoundError as exc:
                ch.error("Missing asset", f"{exc} — plates/voice for {n} live in the workspace")
                raise typer.Exit(1) from exc
    if as_json:
        ch.raw_print(_json.dumps({"rendered": rendered, "workspace": str(ws)}, indent=2, ensure_ascii=False))


@kinetic_app.command("plan")
def kinetic_plan(
    episode: str = typer.Argument(..., help="Episode id."),
    lang: str = typer.Option("en", "--lang", help="Language tag."),
    out: Optional[Path] = typer.Option(None, "--out", help="Output root (default: <workspace>/out/kinetic)."),
    workspace: Optional[Path] = typer.Option(None, "--workspace", "-C", help="Channel workspace."),
    as_json: bool = typer.Option(True, "--json/--plain", help="Timeline as JSON (default)."),
) -> None:
    """🗓 The timeline without rendering — beat times, words, plates, cuts.

    Uses the shared voice cache: a cold line synthesizes (and bills) exactly
    once, the same as the first render would.
    """
    from navig_video.kinetic.ctx import bind
    from navig_video.kinetic.timeline import plan
    from navig_video.workspace import default_workspace, load_workspace_module

    _engines_or_exit()
    ws = default_workspace(workspace)
    if not ws.is_dir():
        ch.error("Workspace not found", f"{ws} — pass --workspace <dir> or set NAVIG_WORKSPACE")
        raise typer.Exit(2)
    bind(ws, resolve_user_path(out) if out else None)
    EP = load_workspace_module(ws, "episodes")
    if episode not in EP.EPISODES:
        ch.error("Unknown episode", f"{episode!r} is not in the workspace")
        raise typer.Exit(2)
    vals, _ = _vals(ws, [episode], [lang])
    try:
        tl = plan(episode, lang, vals)
    except KeyError as exc:
        ch.error("No such language for this episode", f"{episode} has no say\\[{lang}]")
        raise typer.Exit(2) from exc
    data = {
        "episode": episode,
        "lang": lang,
        "total": round(tl.total, 3),
        "end_t0": round(tl.end_t0, 3),
        "open_until": round(tl.open_until, 3),
        "motion": tl.motion,
        "beats": [
            {
                "t0": round(b.t0, 3), "t1": round(b.t1, 3),
                "plate": b.plate, "cut": b.cut, "sfx": b.sfx,
                "fx": b.fx, "bar": b.bar, "seg": b.seg,
                "audio": b.audio.name,
                "words": [{"w": w["w"], "s": round(w["s"], 3),
                           "e": round(w["e"], 3), "red": w["red"]} for w in b.words],
            }
            for b in tl.beats
        ],
    }
    if as_json:
        ch.raw_print(_json.dumps(data, ensure_ascii=False, indent=2))
        return
    for b in data["beats"]:
        ch.raw_print(f"{b['t0']:7.2f}–{b['t1']:7.2f}  {b['plate']:12s} {b['cut']:6s} " +
                     " ".join(w["w"] + ("*" if w["red"] else "") for w in b["words"]))
    ch.dim(f"total {data['total']}s · {len(data['beats'])} beats")
