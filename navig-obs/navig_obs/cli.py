"""Standalone entry point for the ``navig-obs`` console script.

Installs and runs without navig: the same ``obs_app`` Typer app navig mounts
as ``navig obs``. One command surface, two front doors.
"""

from __future__ import annotations

from navig_sdk.cli import run

from navig_obs.commands.obs import obs_app


def main() -> None:
    run(obs_app, "navig-obs")


if __name__ == "__main__":
    main()
