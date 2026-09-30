"""Small console helpers built on rich (which ships with navig-core)."""

from __future__ import annotations

from typing import Any, Iterable

try:
    from rich.console import Console
    from rich.table import Table

    _console = Console()
    _RICH = True
except Exception:  # pragma: no cover — rich should always be present with navig-core
    _console = None
    _RICH = False


def out(message: str = "") -> None:
    if _RICH and _console is not None:
        _console.print(message)
    else:  # pragma: no cover
        print(_strip(message))


def ok(message: str) -> None:
    out(f"[green]✓[/green] {message}" if _RICH else f"OK {message}")


def warn(message: str) -> None:
    out(f"[yellow]⚠[/yellow] {message}" if _RICH else f"WARN {message}")


def err(message: str) -> None:
    out(f"[red]✗[/red] {message}" if _RICH else f"ERROR {message}")


def table(title: str, columns: list[str], rows: Iterable[Iterable[Any]]) -> None:
    if _RICH and _console is not None:
        t = Table(title=title)
        for col in columns:
            t.add_column(col)
        for row in rows:
            t.add_row(*[str(c) for c in row])
        _console.print(t)
    else:  # pragma: no cover
        print(title)
        print(" | ".join(columns))
        for row in rows:
            print(" | ".join(str(c) for c in row))


def _strip(message: str) -> str:
    import re

    return re.sub(r"\[/?[a-zA-Z0-9 =#_]+\]", "", message)
