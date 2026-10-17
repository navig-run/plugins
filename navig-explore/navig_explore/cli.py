"""Standalone entry point for the ``navig-explore`` console script.

Installs and runs without navig: the same ``explore_app`` Typer app navig mounts as
``navig explore``. One command surface, two front doors.
"""

from __future__ import annotations

from navig_sdk.cli import run

from navig_explore.commands.explore import explore_app


def main() -> None:
    run(explore_app, "navig-explore")


if __name__ == "__main__":
    main()
