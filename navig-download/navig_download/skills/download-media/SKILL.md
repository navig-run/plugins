---
name: download-media
description: Download a video, audio track, playlist, reel, or short from a URL (YouTube, TikTok, and 1000+ sites). Use when the user wants to download, save, grab, or rip a video or audio file from a link.
activation_keywords: [download, tiktok, youtube]
activation_keywords_ru: [скача, скачив, ютуб, тикток, рилс]
activation_keywords_fr: [telecharg]
metadata:
  version: 1.0.0
  toolsAllowed:
    - bash_exec
---

# Download Media from a URL

This capability is provided by the **navig-download** plugin (yt-dlp-backed):

- `navig download download <url…>` — download one or more posts (organized, resumable).
- `navig download batch <file>` — every URL listed in a links file.
- `navig download profile <user>` — a creator's whole profile.

(`dl`, `tiktok` and `tt` are hidden aliases of the `download` verb.)

Run `navig download download --help` for format, quality, and output options before
running.

Notes:
- Confirm the destination directory with the user; never overwrite existing files silently.
- For a page of many links, prefer the batch options over one call per URL.
