"""navig-bookmarks — a local bookmark manager that runs on its own or as ``navig bookmarks``.

Import from any browser or Netscape HTML export (folders kept), search everything with
SQLite FTS5, let your own AI propose a folder tree (dry run first, with a failsafe) or tags
(only ever replacing its own), check for dead links and redirects without touching private
addresses, merge duplicates, and export back to HTML with folders.

Nothing here imports navig at import time: storage, paths, console, AI and the SSRF guard
come from navig-sdk, which uses navig's own implementation when navig is installed.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__", "main"]


def main() -> None:
    from navig_bookmarks.cli import main as _main  # noqa: PLC0415

    _main()
