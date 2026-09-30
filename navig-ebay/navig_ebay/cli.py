"""Standalone entry point for the ``navig-ebay`` console script.

Installs and runs without navig: the same ``ebay_app`` Typer app navig mounts as
``navig ebay``. One command surface, two front doors.
"""

from __future__ import annotations

from navig_sdk.cli import run

from navig_ebay.commands.ebay import ebay_app


def main() -> None:
    run(ebay_app, "navig-ebay")


if __name__ == "__main__":
    main()
