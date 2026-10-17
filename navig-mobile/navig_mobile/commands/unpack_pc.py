"""``navig unpack …`` — turn a flat drop folder of PC game archives into per-game extractions.

Typical use:
  navig unpack doctor                       # toolchain
  navig unpack ingest D:\\pc_games_extract  # classify (read-only) → _manifest.json
  navig unpack run   D:\\pc_games_extract   # extract + export every pending game (resumable)
  navig unpack game  D:\\pc_games_extract "Civilization 1"   # one game
  navig unpack report D:\\pc_games_extract  # batch status

Heavy imports stay inside the commands so ``navig help`` stays fast.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Optional

import typer

from navig_mobile.console import Console
from navig_sdk.host import command_name

CMD = command_name("unpack")
c = Console()

unpack_app = typer.Typer(
    name="unpack",
    help="🖥 PC game archives: classify a drop folder, extract every game into per-game "
         "folders, export text/media/audio by language (video excluded by default).",
    no_args_is_help=True,
)

JsonOpt = Annotated[bool, typer.Option("--json", help="Machine-readable JSON output.")]
VideoOpt = Annotated[bool, typer.Option("--include-video",
                                        help="Keep video files (default: extract everything else)")]
InstallOpt = Annotated[bool, typer.Option("--allow-install",
                                          help="Allow silent installer execution as a last resort "
                                               "(AV scan recommended first)")]


def _show(result, json_out: bool) -> None:
    if json_out:
        typer.echo(json.dumps(result, indent=1, default=str, ensure_ascii=False))
    else:
        c.print_rows(["Key", "Value"], [[k, json.dumps(v, default=str, ensure_ascii=False)[:160]]
                                        for k, v in result.items()])


def _resolve_game(root: Path, query: str) -> Optional[str]:
    """Exact name, unique substring, or None."""
    from navig_mobile.unpack import pc

    games = pc.load_manifest(root).get("games", {})
    if query in games:
        return query
    hits = [n for n in games if query.lower() in n.lower()]
    return hits[0] if len(hits) == 1 else None


@unpack_app.command("doctor")
def cmd_doctor() -> None:
    """Show which external tools and optional Python packages are available."""
    from navig_mobile.unpack import tools

    rows = [[n, p or "MISSING", purpose] for n, p, purpose in tools.status()]
    rows += [[m, "ok" if ok else "MISSING (pip install 'navig-mobile[unpack]')", "python package"]
             for m, ok in tools.python_deps()]
    c.print_rows(["Tool", "Location", "Used for"], rows, title="unpack toolchain")


@unpack_app.command("ingest")
def cmd_ingest(root: Annotated[Path, typer.Argument(help="Drop folder of archives.")],
               json_out: JsonOpt = False) -> None:
    """Classify every archive (volume sets joined, duplicates detected) into _manifest.json."""
    from navig_mobile.unpack import pc

    if not root.is_dir():
        c.error(f"not a folder: {root}")
        raise typer.Exit(2)
    manifest = pc.ingest(root, log=c.step)
    if json_out:
        _show({n: g["status"] for n, g in sorted(manifest["games"].items())}, True)
    else:
        rows = [[n, g["status"], g["kind"], f"{g['size']:,}"] for n, g in
                sorted(manifest["games"].items())]
        c.print_rows(["Game", "Status", "Kind", "Bytes"], rows, title=str(root))
    c.success(f"manifest: {root / pc.MANIFEST_NAME}")


@unpack_app.command("run")
def cmd_run(root: Annotated[Path, typer.Argument(help="Drop folder (ingested first).")],
            only: Annotated[Optional[str], typer.Option("--only", help="Process one game by name.")] = None,
            retry_failed: Annotated[bool, typer.Option("--retry-failed",
                                                       help="Also retry failed / needs-install games.")] = False,
            include_video: VideoOpt = False,
            allow_install: InstallOpt = False,
            json_out: JsonOpt = False) -> None:
    """Extract and export every pending game, one by one (resumable: re-run skips done games)."""
    from navig_mobile.unpack import pc

    if not (root / pc.MANIFEST_NAME).exists():
        c.error(f"{root} was not ingested yet — run `{CMD} ingest {root}` first.")
        raise typer.Exit(2)
    if only:
        resolved = _resolve_game(root, only)
        if resolved is None:
            c.error(f"no unique game matching {only!r} in the manifest.")
            raise typer.Exit(2)
        only = resolved
    summary = pc.run_all(root, include_video=include_video, allow_install=allow_install,
                         retry_failed=retry_failed, only=only, log=c.step)
    _show(summary, json_out)
    c.success(f"report: `{CMD} report {root}` · browse: `navig explore {root / '_games'}'")


@unpack_app.command("game")
def cmd_game(root: Annotated[Path, typer.Argument(help="Drop folder.")],
             game: Annotated[str, typer.Argument(help="Game name (unique substring is enough).")],
             include_video: VideoOpt = False,
             allow_install: InstallOpt = False) -> None:
    """Extract and export exactly one game (re-runs it even if done)."""
    from navig_mobile.unpack import pc

    resolved = _resolve_game(root, game)
    if resolved is None:
        c.error(f"no unique game matching {game!r} — see `{CMD} report {root}`.")
        raise typer.Exit(2)
    pc.run_all(root, include_video=include_video, allow_install=allow_install,
               only=resolved, log=c.step)
    c.success(f"done: {root / pc.GAMES_DIR / pc.safe_name(resolved)}")


@unpack_app.command("report")
def cmd_report(root: Annotated[Path, typer.Argument(help="Drop folder.")],
               json_out: JsonOpt = False) -> None:
    """Batch status: every game, its state, method and languages."""
    from navig_mobile.unpack import pc

    manifest = pc.load_manifest(root)
    games = manifest.get("games", {})
    if json_out:
        _show({n: {k: g.get(k) for k in ("status", "method", "size", "error")} for n, g in
               sorted(games.items())}, True)
        return
    counts: dict[str, int] = {}
    for g in games.values():
        counts[g["status"]] = counts.get(g["status"], 0) + 1
    rows = [[n, g["status"], g.get("method", ""), f"{g['size']:,}",
             (g.get("error") or "")[:60]] for n, g in sorted(games.items())]
    c.print_rows(["Game", "Status", "Method", "Bytes", "Error"], rows, title=str(root))
    c.info(" · ".join(f"{v} {k}" for k, v in sorted(counts.items()))
           + f" · updated {manifest.get('updated', '-')}")
    failed = [n for n, g in games.items() if g["status"] in ("failed", "needs-install")]
    if failed:
        c.error(f"{len(failed)} not extracted: " + ", ".join(sorted(failed)))
        raise typer.Exit(1)
