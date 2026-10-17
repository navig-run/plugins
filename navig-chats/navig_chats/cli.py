"""Standalone entry point for the ``navig-chats`` console script (the same app as ``navig chats``)."""

from __future__ import annotations

from navig_sdk.cli import run

from navig_chats.commands.chats import app


def main() -> None:
    run(app, "navig-chats")


if __name__ == "__main__":
    main()
