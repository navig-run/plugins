"""Standalone entry point for the ``navig-devhost`` console script.

Installs and runs without navig: the same ``devhost_app`` Typer app navig mounts as
``navig devhost``. One command surface, two front doors.
"""

from __future__ import annotations

from navig_sdk.cli import run

from navig_devhost.commands.devhost import devhost_app


def main() -> None:
    run(devhost_app, "navig-devhost")


if __name__ == "__main__":
    main()
