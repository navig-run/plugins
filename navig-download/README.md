# navig-download

NAVIG **Download** — a universal media downloader (yt-dlp) for video, audio & files from
YouTube, TikTok and any supported site. A first-party navig plugin (free, toggleable),
extracted from `navig-media`.

## What it does

- **Download** organized, resumable, concurrent media via the bundled yt-dlp engine.
- **TikTok extras** — metadata, top comments, and AI briefings for TikTok links.
- **Music links** — resolve a Spotify/Apple Music/Deezer/… link to the same track on every
  platform (song.link / Odesli, no API key).

## Commands

```
navig download <url…>       # download (organized <out>/<creator>/<id>)   (alias: dl)
navig download batch <file> # download every URL in a text file
navig download profile <u>  # whole profile (filters, archive, limits)
navig download info <url>   # metadata: creator · country · description · stats
navig download comments <url>
navig download analyse <url> # AI markdown briefing
navig download music-links <url>  # resolve a music link across all 18+ platforms (song.link)
```

`navig tiktok` / `tt` remain as back-compat aliases of the same app.

## Install

Bundled with a full `navig` install (the `download` extra); standalone:
`navig store install pip:navig-download` — or `pip install navig-download` once published.
Requires `navig-core` in the same environment.
