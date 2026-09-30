"""Standalone entry point for the ``navig-calendar`` console script.

Installs and runs without navig: the same ``calendar_app`` Typer app navig mounts as
``navig calendar``. One command surface, two front doors.
"""

from __future__ import annotations

from navig_sdk.cli import run

from navig_calendar.commands.calendar import calendar_app


def main() -> None:
    run(calendar_app, "navig-calendar")


if __name__ == "__main__":
    main()
