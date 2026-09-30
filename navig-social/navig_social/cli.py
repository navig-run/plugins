"""Standalone entry point for the ``navig-social`` console script.

Installs and runs without navig: ``navig social``'s commands, plus the ``facebook`` Page tools
(``navig facebook`` inside navig) as a group. One command surface, two front doors.
"""

from __future__ import annotations

import typer
from navig_sdk.cli import run

from navig_social.commands.facebook import facebook_app
from navig_social.commands.social import social_app


def _standalone_app() -> typer.Typer:
    """``navig social``'s commands and groups, plus ``facebook`` — without changing either app."""
    app = typer.Typer(name="navig-social", help=social_app.info.help, no_args_is_help=True)
    app.registered_commands.extend(social_app.registered_commands)
    app.registered_groups.extend(social_app.registered_groups)
    app.add_typer(facebook_app, name="facebook")
    return app


def main() -> None:
    run(_standalone_app(), "navig-social")


if __name__ == "__main__":
    main()
