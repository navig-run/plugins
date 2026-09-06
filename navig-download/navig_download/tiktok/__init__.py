"""NAVIG TikTok — download + rich metadata + AI briefings for TikTok content.

Two layers, both bundled with navig-download, both called **in-process**:

- **the download engine** (`navig_download.downloader`) — concurrent, organized
  downloads, fully wired: `navig tiktok download|batch|profile`.
- **yt-dlp** (the engine's backend) — used directly for rich metadata
  (description, country, stats) + top comments, which feed an AI **briefing**.

Imports stay lazy (the lazy-import law): importing this package never pulls the
downloader or yt-dlp; only the specific call that needs them will raise.
"""
from __future__ import annotations


def downloader_available() -> bool:
    """True if the bundled download engine is importable."""
    try:
        import navig_download.downloader  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


def ytdlp_available() -> bool:
    """True if `yt_dlp` is importable (the download engine's backend)."""
    try:
        import yt_dlp  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False
