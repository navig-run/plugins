"""Standalone entry point for the ``navig-devnet`` console script.

Installs and runs without navig: the same ``devnet_app`` Typer app navig mounts
as ``navig devnet``. One command surface, two front doors.
"""

from __future__ import annotations

from navig_sdk.cli import run

from navig_devnet.commands.devnet import devnet_app


def main() -> None:
    run(devnet_app, "navig-devnet")


if __name__ == "__main__":
    main()
