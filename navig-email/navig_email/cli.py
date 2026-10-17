"""Standalone entry point for the ``navig-email`` console script.

Installs and runs without navig: the same ``email_app`` Typer app navig mounts as
``navig email``. Gmail comes through an app password over IMAP (``navig-email imap connect``).
"""

from __future__ import annotations

from navig_sdk.cli import run

from navig_email.commands.email import email_app


def main() -> None:
    run(email_app, "navig-email")


if __name__ == "__main__":
    main()
