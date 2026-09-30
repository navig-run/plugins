"""Standalone entry point for the ``navig-mobile`` console script.

Installs and runs without navig: the same ``mobile_app`` Typer app navig mounts as
``navig mobile`` (``navig-mobile android …`` / ``ios …`` included). One surface, two doors.
"""

from __future__ import annotations

from navig_sdk.cli import run

from navig_mobile.commands.mobile import mobile_app


def main() -> None:
    run(mobile_app, "navig-mobile")


if __name__ == "__main__":
    main()
