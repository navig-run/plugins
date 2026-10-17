"""``navig-blackbox`` / ``nbb`` — a standalone flight-recorder & incident toolkit.

Record events, capture crashes (``install_crash_handler``), and seal/export ``.navbox``
bundles for postmortems — with or without navig installed. Engines are imported lazily so
``--help`` stays instant.
"""
from __future__ import annotations

from pathlib import Path

import typer

blackbox_app = typer.Typer(
    name="blackbox",
    help="🛰️ Flight-recorder & crash black-box — record events, seal & export .navbox bundles.",
    no_args_is_help=True,
    add_completion=False,
)


def _console():
    from navig_blackbox._compat import get_console

    return get_console()


@blackbox_app.callback()
def _root() -> None:
    """Flight-recorder & crash black-box — non-destructive incident capture."""


@blackbox_app.command()
def status() -> None:
    """Show recorder stats — event count, on-disk size, sealed state, and data dir."""
    from navig_blackbox._compat import blackbox_dir
    from navig_blackbox.recorder import get_recorder
    from navig_blackbox.seal import is_sealed

    rec = get_recorder()
    d = blackbox_dir()
    c = _console()
    c.print(f"[b]Blackbox[/b]  [dim]{d}[/dim]")
    c.print(f"  events      [b]{rec.event_count()}[/b]")
    c.print(f"  size        [b]{rec.file_size_mb():.2f} MB[/b]")
    c.print(f"  sealed      {'[red]yes[/red]' if is_sealed(d) else '[green]no[/green]'}")
    last = rec.last_event_ts()
    c.print(f"  last event  {last.isoformat() if last else '[dim]—[/dim]'}")


@blackbox_app.command()
def events(
    n: int = typer.Option(50, "-n", "--limit", help="How many recent events to show."),
) -> None:
    """Render the most recent recorded events as a timeline."""
    from navig_blackbox.recorder import get_recorder
    from navig_blackbox.timeline import render_timeline

    recent = list(reversed(get_recorder().tail(n)))  # oldest-first for the timeline
    render_timeline(recent, limit=n, console=_console())


@blackbox_app.command()
def record(
    event_type: str = typer.Argument(..., help="Event type: command|session|output|warning|error|crash|system."),
    message: str = typer.Argument(..., help="A short message to record."),
    source: str = typer.Option("cli", "--source", help="Event source label."),
) -> None:
    """Record a single event into the flight recorder."""
    from navig_blackbox.recorder import get_recorder
    from navig_blackbox.types import EventType

    try:
        et = EventType[event_type.upper()]
    except KeyError as exc:
        valid = ", ".join(e.name.lower() for e in EventType)
        _console().print(f"[red]✗ Unknown event type[/red] '{event_type}'. Valid: {valid}")
        raise typer.Exit(1) from exc
    # `record()` returns None when it did NOT record — do not print a receipt
    # for an event that was dropped (a sealed blackbox refuses appends).
    if get_recorder().record(et, {"message": message}, source=source) is None:
        from navig_blackbox._compat import blackbox_dir
        from navig_blackbox.seal import is_sealed

        why = (
            "the blackbox is sealed — run `navig blackbox seal --unseal` first"
            if is_sealed(blackbox_dir())
            else "recording is disabled"
        )
        _console().print(f"[red]✗ not recorded[/red] — {why}")
        raise typer.Exit(1)
    _console().print(f"[green]✓ recorded[/green] [b]{et.name.lower()}[/b]: {message}")


@blackbox_app.command()
def bundle(
    output: Path = typer.Option(Path("incident.navbox"), "-o", "--output", help="Output .navbox path."),
    hours: float = typer.Option(24.0, "--hours", help="Include events from the last N hours."),
    encrypt: bool = typer.Option(False, "--encrypt", help="Encrypt with the navig vault key (navig only; falls back to plaintext)."),
) -> None:
    """Create a .navbox incident bundle from recent events, crashes & log tails."""
    from navig_blackbox.bundle import create_bundle
    from navig_blackbox.export import export_bundle

    b = create_bundle(since_hours=hours)
    path = export_bundle(b, output, encrypted=encrypt)
    _console().print(
        f"[green]✓ Wrote bundle[/green] → [b]{path}[/b]  "
        f"[dim]({b.event_count()} event(s), {b.crash_count()} crash(es))[/dim]"
    )


@blackbox_app.command()
def inspect(
    path: Path = typer.Argument(..., exists=True, dir_okay=False, help="A .navbox file to inspect."),
) -> None:
    """Inspect a .navbox bundle — manifest summary + its event timeline."""
    from navig_blackbox.bundle import inspect_bundle
    from navig_blackbox.timeline import render_timeline

    b = inspect_bundle(path)
    c = _console()
    c.print(
        f"[b]{path.name}[/b]  id={b.id}  created={b.created_at.isoformat()}  "
        f"navig={b.navig_version}  sealed={b.sealed}"
    )
    c.print(f"  [dim]{b.event_count()} event(s) · {b.crash_count()} crash(es) · hash {b.manifest_hash[:12]}…[/dim]")
    render_timeline(b.events, console=c)


@blackbox_app.command()
def crashes() -> None:
    """List captured crash reports (newest first)."""
    from navig_blackbox.crash import list_crashes

    reports = list_crashes()
    c = _console()
    if not reports:
        c.print("[green]✓ No crash reports.[/green]")
        return
    from rich.table import Table

    t = Table(box=None, show_header=True, header_style="bold", padding=(0, 2))
    t.add_column("When", no_wrap=True)
    t.add_column("Signal", no_wrap=True)
    t.add_column("Exception", overflow="fold")
    for r in reports:
        t.add_row(r.timestamp, r.signal_name, f"{r.exception_type}: {r.exception_msg}")
    c.print(t)


@blackbox_app.command()
def seal() -> None:
    """Seal the blackbox — mark the current state immutable for incident preservation."""
    from navig_blackbox.bundle import create_bundle
    from navig_blackbox.seal import seal_bundle

    seal_bundle(create_bundle(since_hours=0.001))
    _console().print("[green]✓ Sealed[/green] [dim]— recording is now frozen until `unseal`.[/dim]")


@blackbox_app.command()
def unseal() -> None:
    """Remove the seal, allowing recording to resume."""
    from navig_blackbox.seal import unseal as _unseal

    removed = _unseal()
    _console().print(
        "[green]✓ Unsealed[/green]" if removed else "[dim]Already unsealed (no marker).[/dim]"
    )
