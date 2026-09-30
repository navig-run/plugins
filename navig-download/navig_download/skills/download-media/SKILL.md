---
name: download-media
description: Download a video, audio track, playlist, reel, or short from a URL (YouTube, TikTok, and 1000+ sites). Use when the user wants to download, save, grab, or rip a video or audio file from a link.
activation_keywords: [download, tiktok, youtube]
metadata:
  version: 1.0.0
  toolsAllowed:
    - bash_exec
---

# Download Media from a URL

This capability is provided by the **navig-download** plugin (yt-dlp-backed):

- `navig download <url>` (alias `navig dl`) — universal downloader for most sites.
- `navig tiktok <url>` (alias `navig tt`) — TikTok-specific download (profiles, batches).

Run `navig download --help` (or `navig tiktok --help`) for format, quality, and
output options before running.

Notes:
- Confirm the destination directory with the user; never overwrite existing files silently.
- For a page of many links, prefer the batch options over one call per URL.
