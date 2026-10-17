"""navig-chats — a local library for your AI conversations, on its own or as ``navig chats``.

Import ChatGPT's account export, save chats from the page with the NAVIG Dock's ChatGPT Lens,
search every message with SQLite FTS5, organise chats in NAVIG folders (a local overlay — never
ChatGPT's own Projects), and export them in formats that stay readable without NAVIG.

Nothing here imports navig at import time: storage, paths and console come from navig-sdk,
which uses navig's own implementation when navig is installed.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__", "main"]


def main() -> None:
    from navig_chats.cli import main as _main  # noqa: PLC0415

    _main()
