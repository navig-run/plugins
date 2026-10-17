"""Standalone entry point for the ``navig-notes`` console script (the same app as ``navig notes``)."""

from __future__ import annotations

from navig_sdk.cli import run

from navig_notes.commands.notes import app


def main() -> None:
    run(app, "navig-notes")


if __name__ == "__main__":
    main()
