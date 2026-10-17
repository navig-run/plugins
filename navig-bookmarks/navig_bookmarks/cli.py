"""Standalone entry point for the ``navig-bookmarks`` console script.

The same ``app`` navig mounts as ``navig bookmarks``: one command surface, two front doors,
one bookmark store.
"""

from __future__ import annotations

from navig_sdk.cli import run

from navig_bookmarks.commands.bookmarks import app


def main() -> None:
    run(app, "navig-bookmarks")


if __name__ == "__main__":
    main()
