"""Standalone entry point for the ``navig-browser`` console script.

Installs and runs without navig: the commands navig mounts on ``navig cdp`` — launch an
isolated browser, drive it, profiles, and ``stop`` (which only ever closes browsers this
engine launched). One command surface, two front doors.
"""

from __future__ import annotations

from navig_sdk.cli import run

from navig_browser.commands.cdp import cdp_app


def main() -> None:
    run(cdp_app, "navig-browser")


if __name__ == "__main__":
    main()
