"""Standalone entry point for the ``navig-games`` console script.

Installs and runs without navig: the same commands navig mounts on ``navig games``. Epic
sign-in / auto-claim and ``schedule`` need navig and say so. One command surface, two doors.
"""

from __future__ import annotations

from navig_sdk.cli import run

from navig_games.commands.games import games_app


def main() -> None:
    run(games_app, "navig-games")


if __name__ == "__main__":
    main()
