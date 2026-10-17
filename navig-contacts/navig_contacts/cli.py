"""Standalone entry point for the ``navig-contacts`` console script.

Installs and runs without navig: the same ``app`` Typer app navig mounts as
``navig contacts``. One command surface, two front doors, one book.
"""

from __future__ import annotations

from navig_sdk.cli import run

from navig_contacts.commands.contacts import app


def main() -> None:
    run(app, "navig-contacts")


if __name__ == "__main__":
    main()
