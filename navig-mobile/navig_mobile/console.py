"""Console output helper — prefers ``navig.console_helper`` (Rich, house style),
falls back to plain ``typer`` output so the plugin also works standalone.

Mirrors the ``_Console`` shim in navig-antivirus; adds a small Rich ``Table``
passthrough so list/status verbs render as proper tables when Rich is available.
"""

from __future__ import annotations

from typing import Any

import typer


class Console:
    def __init__(self) -> None:
        self._ch = None
        try:
            from navig.lazy_loader import lazy_import

            self._ch = lazy_import("navig.console_helper")
        except Exception:
            try:
                import navig.console_helper as ch  # type: ignore

                self._ch = ch
            except Exception:
                self._ch = None

    # ── single-line messages ────────────────────────────────────────────────
    def _emit(self, fn: str, msg: str, color) -> None:
        f = getattr(self._ch, fn, None) if self._ch else None
        if callable(f):
            try:
                f(msg)
                return
            except Exception:
                pass
        typer.secho(msg, fg=color)

    def success(self, m: str) -> None:
        self._emit("success", m, typer.colors.GREEN)

    def error(self, m: str) -> None:
        self._emit("error", m, typer.colors.RED)

    def warning(self, m: str) -> None:
        self._emit("warning", m, typer.colors.YELLOW)

    def info(self, m: str) -> None:
        self._emit("info", m, typer.colors.CYAN)

    def step(self, m: str) -> None:
        self._emit("step", m, typer.colors.CYAN)

    def dim(self, m: str) -> None:
        typer.secho(m, fg=typer.colors.BRIGHT_BLACK)

    def plain(self, m: str = "") -> None:
        typer.echo(m)

    def styled(self, text: str, style: str) -> Any:
        """A table cell that renders with a Rich style (green/yellow/red…) and
        degrades to plain text without Rich. Use this instead of embedding
        ``[green]…[/green]`` markup in data (literal ``[android]`` would be eaten
        by Rich's markup parser)."""
        try:
            from rich.text import Text

            return Text(text, style=style)
        except Exception:
            return text

    # ── tables ──────────────────────────────────────────────────────────────
    def print_rows(
        self,
        columns: list[str],
        rows: list[list[Any]],
        *,
        title: str | None = None,
    ) -> None:
        """Render a table via Rich when available; degrade to aligned text.

        Plain-string cells are markup-escaped so literal brackets (e.g. the
        ``[android]`` extra in an install hint) survive; pass ``self.styled(...)``
        for cells that should carry colour.
        """
        console = getattr(self._ch, "console", None) if self._ch else None
        Table = getattr(self._ch, "Table", None) if self._ch else None
        if console is not None and Table is not None:
            try:
                from rich.markup import escape

                table = Table(title=title, box=None, show_header=True, padding=(0, 2),
                              header_style="bold cyan")
                for col in columns:
                    table.add_column(col, no_wrap=(col != columns[-1]))
                for row in rows:
                    table.add_row(*[self._rich_cell(cell, escape) for cell in row])
                console.print(table)
                return
            except Exception:
                pass
        # Fallback: fixed-width aligned text.
        if title:
            typer.secho(title, fg=typer.colors.CYAN, bold=True)
        widths = [len(c) for c in columns]
        srows = [[self._plain_cell(c) for c in row] for row in rows]
        for row in srows:
            for i, cell in enumerate(row):
                widths[i] = max(widths[i], len(cell))
        typer.secho("  ".join(c.ljust(widths[i]) for i, c in enumerate(columns)),
                    fg=typer.colors.CYAN, bold=True)
        for row in srows:
            typer.echo("  ".join(c.ljust(widths[i]) for i, c in enumerate(row)))

    @staticmethod
    def _rich_cell(value: Any, escape) -> Any:
        # Pass Rich renderables (e.g. Text from styled()) through untouched;
        # escape plain strings so literal [brackets] aren't parsed as markup.
        if value.__class__.__name__ == "Text":
            return value
        return escape("" if value is None else str(value))

    @staticmethod
    def _plain_cell(value: Any) -> str:
        if value.__class__.__name__ == "Text":
            return getattr(value, "plain", str(value))
        return "" if value is None else str(value)
