"""Standalone entry point for the ``navig-text`` console script.

Inside navig this plugin adds two verbs, ``navig text`` and ``navig design``. On its own it
is one command: ``navig-text gen|check`` plus ``navig-text lyrics …`` and
``navig-text design …``. The combined app is built here only, so navig's own surfaces are
unchanged.
"""

from __future__ import annotations

import typer
from navig_sdk.cli import run

from navig_text.commands.design import design_app
from navig_text.commands.lyrics import lyrics_app
from navig_text.commands.text import text_app

app = typer.Typer(
    name="navig-text",
    help="📝 AI text generation and 🎨 AI design edits — with navig's AI or your own.",
    no_args_is_help=True,
)
app.registered_commands += text_app.registered_commands
app.add_typer(lyrics_app, name="lyrics")
app.add_typer(design_app, name="design")


def main() -> None:
    run(app, "navig-text")


if __name__ == "__main__":
    main()
