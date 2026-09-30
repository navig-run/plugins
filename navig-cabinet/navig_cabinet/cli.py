"""Standalone entry point for the ``navig-cabinet`` console script.

Inside navig this plugin adds two verbs, ``navig cabinet`` and ``navig paperwork``. On its
own it is one command: ``navig-cabinet …`` plus ``navig-cabinet paperwork …``. The combined
app is built here only, so navig's own surfaces are unchanged.
"""

from __future__ import annotations

import typer
from navig_sdk.cli import run

from navig_cabinet.commands.cabinet import cabinet_app
from navig_cabinet.paperwork.commands.paperwork import paperwork_app

app = typer.Typer(
    name="navig-cabinet",
    help=cabinet_app.info.help,
    no_args_is_help=True,
)
app.registered_commands += cabinet_app.registered_commands
app.registered_groups += cabinet_app.registered_groups
app.add_typer(paperwork_app, name="paperwork")


def main() -> None:
    run(app, "navig-cabinet")


if __name__ == "__main__":
    main()
