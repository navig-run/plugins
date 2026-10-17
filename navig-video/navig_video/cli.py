"""Standalone entry point for the ``navig-video`` console script.

Inside navig this plugin adds one verb, ``navig video``. On its own it is the same
app: ``navig-video status|doctor|…``. Built here only, so navig's own surfaces are
unchanged.
"""

from __future__ import annotations

import typer
from navig_sdk.cli import run

from navig_video.commands.video import video_app


def _standalone_app() -> typer.Typer:
    """``navig video``'s commands, flattened: ``navig-video status``, not ``… video status``."""
    app = typer.Typer(
        name="navig-video",
        help=video_app.info.help,
        no_args_is_help=True,
    )
    app.registered_commands.extend(video_app.registered_commands)
    app.registered_groups.extend(video_app.registered_groups)
    return app


def main() -> None:
    run(_standalone_app(), "navig-video")


if __name__ == "__main__":
    main()
