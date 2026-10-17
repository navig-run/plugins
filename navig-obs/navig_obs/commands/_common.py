"""Shared CLI helpers for `navig obs` (target state, request wrapper, output).

House rules honored here:
- exit honesty: every failure path raises typer.Exit(1/2) via :func:`fail`;
  an unreachable OBS is a *skip* (exit 0) only where the command is a status
  probe answering "not running".
- `--json` everywhere scripts might parse (each command carries its own flag).
- Rich tables: box=None, padding (0,2), one wrappable free-text column.
"""

from __future__ import annotations

import json as _json
from typing import Any, Callable

import typer
from rich.console import Console
from rich.table import Table

from navig_obs.engine.client import ObsClient
from navig_obs.engine.config import ObsTarget, resolve_target
from navig_obs.engine.errors import ObsAuthError, ObsConnectionError, ObsRequestError

console = Console()

# Resolved by the group callback on every invocation (so tests never inherit
# the previous run's target).
_TARGET: ObsTarget = resolve_target()


def set_target(t: ObsTarget) -> None:
    global _TARGET
    _TARGET = t


def target() -> ObsTarget:
    return _TARGET


def fail(msg: str, code: int = 1) -> None:
    """Report a failure on the console and exit non-zero (exit-honesty seam)."""
    console.print(f"[red]✗[/red] {msg}")
    raise typer.Exit(code)


def notice(msg: str) -> None:
    """Neutral skip line — never used for a failure (exit code must agree)."""
    console.print(f"[yellow]○[/yellow] {msg}")


def ok(msg: str) -> None:
    console.print(f"[green]●[/green] {msg}")


def request(rtype: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Issue one obs-websocket request against the resolved target.

    Every failure announces AND exits (1) — callers never fall through on error.
    Connection failures carry the fix hint (OBS running + websocket enabled).
    """
    t = target()
    try:
        with ObsClient(t.host, t.port, t.password) as client:
            return client.request(rtype, data)
    except ObsAuthError as exc:
        fail(f"{exc}")
    except ObsConnectionError as exc:
        fail(
            f"{exc} — is OBS running with the WebSocket server enabled? "
            "(Tools → WebSocket Server Settings, or `navig obs doctor`)"
        )
    except ObsRequestError as exc:
        fail(str(exc))
    return {}  # unreachable: fail() always raises


# Every engine failure class, as one catchable tuple (never a bare except).
ObsErrorAll = (ObsAuthError, ObsConnectionError, ObsRequestError)


def request_optional(rtype: str, data: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Like :func:`request` but returns None instead of exiting — for probes
    whose request an older OBS may not implement, or that decorate a status view."""
    t = target()
    try:
        with ObsClient(t.host, t.port, t.password) as client:
            return client.request(rtype, data)
    except ObsErrorAll:
        return None


def emit(data: Any, renderer: Callable[[Any], None] | None = None, *, as_json: bool) -> None:
    """Print a response as JSON, or hand it to the Rich renderer.

    An empty payload with no renderer prints nothing — the verb's own success
    line (`ok(...)`) is the output there; `{}` would only be noise.
    """
    if as_json:
        console.print_json(_json.dumps(data, default=str))
        return
    if renderer is not None:
        renderer(data)
        return
    if not data:
        return
    console.print_json(_json.dumps(data, default=str))


def kv_table(title: str, rows: list[tuple[str, str]]) -> Table:
    """Two-column field/value table (status views)."""
    table = Table(
        box=None,
        show_header=True,
        padding=(0, 2),
        title=title,
        title_justify="left",
    )
    table.add_column("field", style="cyan", no_wrap=True)
    table.add_column("value")
    for field, value in rows:
        table.add_row(field, value)
    return table


def grid(
    title: str,
    columns: list[tuple[str, bool]],
    rows: list[list[str]],
    *,
    empty: str,
) -> None:
    """N-column list table. ``columns`` = (name, no_wrap); exactly ONE column
    should have no_wrap=False (the wrappable free-text column)."""
    if not rows:
        console.print(f"[yellow]{empty}[/yellow]")
        return
    table = Table(box=None, show_header=True, padding=(0, 2), title=title, title_justify="left")
    for name, no_wrap in columns:
        table.add_column(name, no_wrap=no_wrap)
    for row in rows:
        table.add_row(*row)
    console.print(table)
    console.print(f"[dim]{len(rows)} row(s)[/dim]")
