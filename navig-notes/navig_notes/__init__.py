"""navig-notes — your web sticky notes on this computer, on its own or as ``navig notes``.

The NAVIG Dock keeps notes on web pages (per page, per site, everywhere) in the browser and
syncs them here, to ``<navig data dir>/notes.db``: list, search, add and export them from the
command line, and let NAVIG's Recall find them — only at the sharing level the person chose.

Nothing here imports navig at import time: storage, paths and console come from navig-sdk,
which uses navig's own implementation when navig is installed.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__", "main"]


def main() -> None:
    from navig_notes.cli import main as _main  # noqa: PLC0415

    _main()
