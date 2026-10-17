"""``navig text channel`` — a channel's master texts: sync, lint, sweep, live data.

  navig text channel sync                 # dry report: what the master would change
  navig text channel sync --apply         # write the scripts
  navig text channel sync --check         # exit 1 if pending swaps (CI-able)
  navig text channel lint                 # the master's own declared rules
  navig text channel sweep                # terminology sweep (cities → Zones), dry
  navig text channel data --strict        # the live numbers the cards quote

Everything reads and writes a WORKSPACE (``--workspace/-C``, env ``NAVIG_WORKSPACE``,
default the channel directory) — media and copy never move into navig; only the code
that reads them lives here. Exit codes: 2 = usage (master missing/unparseable, bad
flag), 1 = operation found problems, 0 = clean.
"""

from __future__ import annotations

import json as _json
from pathlib import Path
from typing import Optional

import typer
from navig_sdk import console as ch
from navig_sdk.host import command_name

from navig_text.workspace import default_workspace, resolve_master

CMD = command_name("text", standalone="navig-text")

channel_app = typer.Typer(
    name="channel",
    help="📡 Channel texts — sync the master into the scripts, lint it, sweep terms, read live data.",
    no_args_is_help=True,
)


def _workspace(ws: Optional[Path]) -> Path:
    path = default_workspace(ws)
    if not path.is_dir():
        ch.error("Workspace not found", f"{path} — pass --workspace <dir> or set NAVIG_WORKSPACE")
        raise typer.Exit(2)
    return path


def _master_path(ws: Path, master: Optional[Path]) -> Path:
    try:
        from navig_text.channel.parse import parse_master

        path = resolve_master(ws, master)
        parse_master(path)  # existence/shape probe — the caller re-parses
        return path
    except Exception as exc:  # MasterError or unreadable
        ch.error("Master text not usable", str(exc))
        raise typer.Exit(2) from exc


@channel_app.command("sync")
def channel_sync(
    workspace: Optional[Path] = typer.Option(None, "--workspace", "-C", help="Channel workspace directory."),
    master: Optional[Path] = typer.Option(None, "--master", help="Master text (default: workspace's newest ALL-TEXTS*.md)."),
    only: Optional[str] = typer.Option(None, "--only", help="episodes | tx | lore | posts | txlines"),
    apply: bool = typer.Option(False, "--apply", help="Write the scripts (default: dry report)."),
    check: bool = typer.Option(False, "--check", help="Exit 1 if anything is pending (CI mode)."),
    as_json: bool = typer.Option(False, "--json", help="Emit the report as JSON."),
) -> None:
    """Push the master's values into the channel's scripts, positional by structure."""
    if apply and check:
        ch.error("--apply and --check are mutually exclusive", "--check only reports; --apply writes")
        raise typer.Exit(2)
    ws = _workspace(workspace)
    path = _master_path(ws, master)
    from navig_text.channel.parse import parse_master
    from navig_text.channel.sync import Syncer

    try:
        parsed = parse_master(path)
        syncer = Syncer(ws, parsed, path, apply=apply, only=only)
        report = syncer.run()
    except ValueError as exc:  # bad --only
        ch.error("Bad --only", str(exc))
        raise typer.Exit(2) from exc
    except OSError as exc:
        ch.error("Sync failed", str(exc))
        raise typer.Exit(1) from exc

    if as_json:
        ch.raw_print(
            _json.dumps(
                {
                    "master": str(path),
                    "applied": report.applied,
                    "stats": report.stats,
                    "problems": report.problems,
                    "pending": report.pending,
                    "lines": report.lines,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        ch.raw_print(report.render())

    if report.problems:
        if not as_json:
            ch.error(f"{report.problems} problem(s) — nothing ambiguous was written")
        raise typer.Exit(1)
    if check and report.pending:
        if not as_json:
            ch.warning(
                "Pending swaps",
                f"{', '.join(report.pending)} differ from the master — run without --check to review",
            )
        raise typer.Exit(1)


@channel_app.command("lint")
def channel_lint(
    workspace: Optional[Path] = typer.Option(None, "--workspace", "-C", help="Channel workspace directory."),
    master: Optional[Path] = typer.Option(None, "--master", help="Master text path."),
    strict: bool = typer.Option(False, "--strict", help="Treat notes as errors."),
    as_json: bool = typer.Option(False, "--json", help="Emit findings as JSON."),
) -> None:
    """Check the master against the rules its own header declares."""
    ws = _workspace(workspace)
    path = _master_path(ws, master)
    from navig_text.channel.lint import lint_master, lint_scripts
    from navig_text.channel.parse import parse_master

    parsed = parse_master(path)
    findings = lint_master(path, parsed, strict=strict)
    findings += lint_scripts(ws, parsed)
    errors = [f for f in findings if f.severity == "error"]

    if as_json:
        ch.raw_print(
            _json.dumps(
                [
                    {"severity": f.severity, "where": f.where, "message": f.message}
                    for f in findings
                ],
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        for f in findings:
            ch.raw_print(f.render())
        if not findings:
            ch.success("Clean", f"{path.name} and its scripts agree")

    if errors:
        if not as_json:
            ch.error(f"{len(errors)} error(s)", f"in {path.name}")
        raise typer.Exit(1)


@channel_app.command("sweep")
def channel_sweep(
    workspace: Optional[Path] = typer.Option(None, "--workspace", "-C", help="Channel workspace directory."),
    master: Optional[Path] = typer.Option(None, "--master", help="Master text path."),
    apply: bool = typer.Option(False, "--apply", help="Write the replacements (default: report)."),
    check: bool = typer.Option(False, "--check", help="Exit 1 if any hit exists (CI mode)."),
    as_json: bool = typer.Option(False, "--json", help="Emit hits as JSON."),
) -> None:
    """Terminology sweep (cities → Zones): literal replacements, reported before written."""
    if apply and check:
        ch.error("--apply and --check are mutually exclusive", "")
        raise typer.Exit(2)
    ws = _workspace(workspace)
    from navig_text.workspace import resolve_master

    path = resolve_master(ws, master)
    if not path.is_file():
        ch.error("Master text not found", str(path))
        raise typer.Exit(2)
    from navig_text.channel.sweep import sweep_config, sweep_file

    rules, scope, allow = sweep_config(ws)
    result = sweep_file(path, rules, apply=apply, scope=scope, allow=allow)

    if as_json:
        ch.raw_print(
            _json.dumps(
                {
                    "file": str(path),
                    "applied": result.applied,
                    "changed": result.changed,
                    "scope": scope,
                    "hits": [
                        {"line": ln, "before": b, "after": a}
                        for ln, b, a in result.hits
                    ],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        ch.raw_print(result.render())

    if result.hits and check:
        if not as_json:
            ch.warning(f"{len(result.hits)} hit(s)", "run without --check to apply")
        raise typer.Exit(1)


@channel_app.command("data")
def channel_data(
    workspace: Optional[Path] = typer.Option(None, "--workspace", "-C", help="Channel workspace directory."),
    strict: bool = typer.Option(False, "--strict", help="Fail instead of falling back to the cache."),
    as_json: bool = typer.Option(False, "--json", help="Emit numbers as JSON (default: table)."),
) -> None:
    """The live numbers the cards quote — loud fetch, disk cache, never silently stale."""
    ws = _workspace(workspace)
    from navig_text.channel.data import DataError, fetch

    try:
        data = fetch(ws, strict=strict)
    except DataError as exc:
        ch.error("Live data unavailable", str(exc))
        raise typer.Exit(1) from exc

    if as_json:
        ch.raw_print(_json.dumps(dict(data), indent=2, ensure_ascii=False))
        return

    if data.source == "cache":
        ch.warning(
            "Cached answer",
            f"{data.get('_ageHours')}h old — numbers may be stale (source: cache)",
        )
    else:
        ch.dim("source: live")
    table = ch.create_table(
        columns=[{"name": "key", "style": "cyan"}, {"name": "value"}]
    )
    for k, v in data.items():
        if not k.startswith("_"):
            table.add_row(k, str(v))
    ch.print_table(table)
