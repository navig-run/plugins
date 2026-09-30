"""Standalone entry point for the ``navig-antivirus`` console script.

Installs and runs without navig: the same ``antivirus_app`` Typer app navig mounts as
``navig antivirus``. One command surface, two front doors.
"""

from __future__ import annotations

from navig_sdk.cli import run

from navig_antivirus.commands.antivirus import antivirus_app


def main() -> None:
    run(antivirus_app, "navig-antivirus")


if __name__ == "__main__":
    main()
