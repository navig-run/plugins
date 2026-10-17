"""
NAVIG TikTok download engine
====================================

A robust, concurrent TikTok content downloader built with Python and yt-dlp.
Download videos, images, and audio with or without watermarks, process multiple
URLs simultaneously, and organize downloads automatically by creator.

Original concept by xsrazy | Enhanced and maintained by miztizm

Features:
---------
- Concurrent Downloads: Process multiple videos simultaneously
- Profile Downloader: Download ALL videos from a user's profile
- Smart Content Filtering: Choose what to download - videos, images, or everything
- Metadata Extraction: Save comprehensive post information as JSON
- Watermark Control: Choose between watermarked or clean versions
- Auto-Organization: Videos sorted into folders by creator username
- Archive Tracking: Prevent re-downloading videos you already have
- Robust Error Handling: Failed downloads are logged with detailed errors

Usage:
------
Command-line interface:
    $ the download engine --profile username
    $ the download engine --links urls.txt
    $ the download engine --profile username --content-type all

For more information, visit: https://github.com/navig-run/core
"""

__version__ = "2.0.0"
__author__ = "miztizm (Original: xsrazy)"
__license__ = "MIT"
__all__ = ["main"]

from .main import main

# Package metadata
__title__ = "the download engine"
__description__ = "A robust, concurrent TikTok content downloader built with Python and yt-dlp"
__url__ = "https://github.com/navig-run/core"
