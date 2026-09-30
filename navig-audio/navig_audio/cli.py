"""Standalone entry point for the ``navig-audio`` console script.

Installs and runs without navig: the commands navig mounts on ``navig audio`` (generate, edit,
beats, podcasts), plus ``voice`` (speak / transcribe / list-voices), which navig mounts on
``navig voice``. One command surface, two front doors.
"""

from __future__ import annotations

import typer
from navig_sdk.cli import run

from navig_audio.commands.audio import audio_app
from navig_audio.commands.voice import voice_app


def _standalone_app() -> typer.Typer:
    """``navig audio``'s commands and groups, plus ``voice`` — without adding it to ``navig audio``."""
    app = typer.Typer(name="navig-audio", help=audio_app.info.help, no_args_is_help=True)
    app.registered_commands.extend(audio_app.registered_commands)
    app.registered_groups.extend(audio_app.registered_groups)
    app.add_typer(voice_app, name="voice")
    return app


def main() -> None:
    run(_standalone_app(), "navig-audio")


if __name__ == "__main__":
    main()
