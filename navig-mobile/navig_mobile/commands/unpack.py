"""``navig mobile unpack …`` — extract data and media from Android games/apps.

Typical use:
  navig mobile unpack game com.chucklefish.stardewvalley -o ./games/stardew   # pull + extract everything
  navig mobile unpack run ./games/stardew --only media                        # re-run on an existing folder
Heavy imports stay inside the commands so ``navig help`` stays fast.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Optional

import typer

from navig_mobile.console import Console
from navig_sdk.host import command_name

CMD = command_name("mobile")
c = Console()

unpack_app = typer.Typer(
    name="unpack",
    help="Extract game/app data and media: Unity (incl. IL2CPP), XNB, Kairosoft, HTML5, Wwise/Bink/DDS, any APK.",
    no_args_is_help=True,
)

UdidOpt = Annotated[Optional[str], typer.Option("--udid", "-u", help="Device serial (auto when only one is connected).")]
OnlyOpt = Annotated[Optional[str], typer.Option("--only", help="data | media — run only half of the extraction.")]
JsonOpt = Annotated[bool, typer.Option("--json", help="Machine-readable JSON output.")]


def _deps_or_exit() -> None:
    from navig_mobile.unpack import tools

    missing = [m for m, ok in tools.python_deps() if not ok]
    if missing:
        c.error(f"Missing Python packages: {', '.join(missing)} — install with: pip install 'navig-mobile[unpack]'")
        raise typer.Exit(3)


def _serial(udid: Optional[str]) -> str:
    from navig_mobile.commands.mobile import _resolve

    return _resolve(udid, "android").udid


def _show(result, json_out: bool) -> None:
    if json_out:
        typer.echo(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    else:
        c.print_rows(["Step", "Result"], [[k, json.dumps(v, default=str, ensure_ascii=False)[:160]] for k, v in result.items()])


@unpack_app.command("doctor")
def cmd_doctor() -> None:
    """Show which external tools and optional Python packages are available."""
    from navig_mobile.unpack import tools

    rows = [[n, p or "MISSING", purpose] for n, p, purpose in tools.status()]
    rows += [[m, "ok" if ok else "MISSING (pip install 'navig-mobile[unpack]')", "python package"] for m, ok in tools.python_deps()]
    c.print_rows(["Tool", "Location", "Used for"], rows, title="unpack toolchain")


@unpack_app.command("setup")
def cmd_setup() -> None:
    """Download vgmstream and Il2CppDumper (GitHub releases) into ~/.navig/tools."""
    from navig_mobile.unpack import tools

    for line in tools.setup():
        c.info(line)


@unpack_app.command("pull")
def cmd_pull(
    package: Annotated[str, typer.Argument(help="Android package id, e.g. com.chucklefish.stardewvalley")],
    out: Annotated[Path, typer.Option("--out", "-o", help="Game folder to create.")],
    udid: UdidOpt = None,
) -> None:
    """Copy APK + splits, OBB data files and Android/data content of a package off the device."""
    from navig_mobile.unpack import pipeline

    try:
        pipeline.pull(_serial(udid), package, out, log=c.step)
    except FileNotFoundError as exc:
        c.error(str(exc))
        raise typer.Exit(1) from exc
    c.success(f"Pulled into {out}")


@unpack_app.command("run")
def cmd_run(
    game: Annotated[Path, typer.Argument(help="Game folder (apk/ + optional obb/, android-data/).")],
    only: OnlyOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """Detect the engine and extract all data and media from a game folder."""
    _deps_or_exit()
    from navig_mobile.unpack import pipeline

    if not any((game / "apk").glob("*.apk")):
        c.error(f"{game}/apk/ has no .apk — use `{CMD} unpack pull` or copy the APK(s) there first.")
        raise typer.Exit(2)
    try:
        result = pipeline.run(game, only=only, log=c.step)
    except FileNotFoundError as exc:  # missing external tool
        c.error(str(exc))
        raise typer.Exit(3) from exc
    _show(result, json_out)
    c.success(f"Report: {game / 'REPORT.md'}")


@unpack_app.command("game")
def cmd_game(
    package: Annotated[str, typer.Argument(help="Android package id installed on the device.")],
    out: Annotated[Path, typer.Option("--out", "-o", help="Game folder to create.")],
    udid: UdidOpt = None,
    only: OnlyOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """Pull a game off the device and extract everything (pull + run)."""
    _deps_or_exit()
    from navig_mobile.unpack import pipeline

    try:
        pipeline.pull(_serial(udid), package, out, log=c.step)
        result = pipeline.run(out, only=only, log=c.step)
    except FileNotFoundError as exc:
        c.error(str(exc))
        raise typer.Exit(1) from exc
    _show(result, json_out)
    c.success(f"Report: {out / 'REPORT.md'}")


@unpack_app.command("detect")
def cmd_detect(game: Annotated[Path, typer.Argument(help="Game folder.")]) -> None:
    """Name the engine(s) of an unpacked game folder."""
    from navig_mobile.unpack import pipeline, unity

    if not (game / "unpacked").exists():
        unity.unpack(game)
    c.info(", ".join(pipeline.detect(game)))


@unpack_app.command("audio")
def cmd_audio(
    src: Annotated[Path, typer.Argument(help="Folder to scan.")],
    dst: Annotated[Path, typer.Argument(help="Output folder (layout preserved).")],
    pattern: Annotated[str, typer.Option("--pattern", help="Glob, e.g. *.wem, *.bnk, *.xwb, *.fsb.")] = "*.wem",
    subsongs: Annotated[bool, typer.Option("--subsongs", help="Containers holding many sounds (banks).")] = False,
    jobs: Annotated[int, typer.Option("--jobs", "-j")] = 16,
) -> None:
    """Batch-convert game audio (Wwise, XACT, FMOD, Bink…) to .ogg via vgmstream + ffmpeg."""
    from navig_mobile.unpack import media

    c.success(str(media.convert_audio(src, dst, pattern, subsongs, jobs)))


@unpack_app.command("carve-ogg")
def cmd_carve(container: Annotated[Path, typer.Argument()], out: Annotated[Path, typer.Argument()]) -> None:
    """Recover every complete Ogg stream from an unknown archive (CRC-verified)."""
    from navig_mobile.unpack import media

    c.success(f"{media.carve_ogg(container, out)} stream(s) → {out}")


@unpack_app.command("kairo")
def cmd_kairo(src: Annotated[Path, typer.Argument(help="Folder with Kairosoft .bin files.")],
              dst: Annotated[Path, typer.Argument()]) -> None:
    """Unpack Kairosoft .bin archives (auto-detects the title's XOR key)."""
    from navig_mobile.unpack import kairo

    c.print_rows(["Archive", "Result"], [[k, v] for k, v in kairo.unpack_dir(src, dst).items()])


@unpack_app.command("xnb")
def cmd_xnb(content: Annotated[Path, typer.Argument(help="XNA/MonoGame Content folder.")],
            out: Annotated[Path, typer.Argument()]) -> None:
    """Decode .xnb tables → JSON and textures → PNG (Stardew Valley and other XNA games)."""
    _deps_or_exit()
    from navig_mobile.unpack import xnb

    c.success(str(xnb.export(content, out)))


@unpack_app.command("dds")
def cmd_dds(src: Annotated[Path, typer.Argument()], dst: Annotated[Path, typer.Argument()]) -> None:
    """Convert every .dds texture to .png."""
    from navig_mobile.unpack import media

    ok, bad = media.dds_to_png(src, dst)
    c.success(f"{ok} converted, {bad} kept as .dds")


@unpack_app.command("classes")
def cmd_classes(game: Annotated[Path, typer.Argument(help="Game folder already run through `unpack run`.")]) -> None:
    """Turn decoded Unity objects into one readable CSV per script class."""
    from navig_mobile.unpack import unity

    c.success(f"{unity.classes_to_csv(game)} class table(s) → {game / 'reports/classes'}")


@unpack_app.command("civ6")
def cmd_civ6(
    data: Annotated[Path, typer.Argument(help="Folder holding Civ VI's Base/ and DLC/ (extracted from the OBBs or a PC install).")],
    out: Annotated[Path, typer.Option("--out", "-o", help="Output folder for the database and reports/.")],
    title: Annotated[str, typer.Option("--title", help="Heading used in the reports.")] = "Civilization VI",
) -> None:
    """Civilization VI: rebuild the in-game database (Gathering Storm + DLC) and export readable lists."""
    from navig_mobile.unpack import civ6_db, civ6_lists

    out.mkdir(parents=True, exist_ok=True)
    db = out / "civ6_gameplay.sqlite"
    c.step("building database (schema → Base → DLC/expansions by LoadOrder)")
    built = civ6_db.build(data, db, title)
    c.step(f"database: {built['actions']} load actions, {built['errors']} rows rejected (see _BuildInfo)")
    res = civ6_lists.export(db, out / "reports", title)
    c.success(f"{res['lists']} lists, {res['tables']} tables, {res['strings']} English strings → {out / 'reports'}")
