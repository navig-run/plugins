---
name: audio-mastering
description: Mix a recorded vocal over a beat and master it into a finished song, or measure a track's loudness (LUFS, true peak). Use when the user has a voice take and an instrumental and wants a mastered song, wants a track louder/quieter/"mastered", or asks how loud a song or reference is.
activation_keywords: [master, mastering, mixdown, "mix my vocal", loudness, LUFS, "true peak"]
# `mixe` / `instrumental` are also English (mixed, "instrumental in"), so French uses phrases.
activation_keywords_ru: [мастеринг, сведи, сведен, свести, вокал, громкост, громче]
activation_keywords_fr: [masteris, mixage, "mixe ma", "mixe ta", "mixe le", "mixe les", "l'instrumental", "un instrumental"]
metadata:
  version: 1.0.0
  toolsAllowed:
    - bash_exec
---

# Mixing and mastering

Provided by the **navig-audio** plugin. Everything is ffmpeg, local, and measured.

- `navig audio master <vocal> --beat <beat>` — vocal over beat → mastered mp3 320k beside
  the vocal (never overwrites; `-take2` …) plus a `.json` sidecar with the graph and numbers.
  - `--preset ritual|boom-bap|collage|clean` (−9 / −10 / −10 / −14 LUFS)
  - `--offset <s>` where the vocal starts in the beat · `--balance <LU>` vocal above beat
  - `--lufs` / `--tp` override the targets · `--wav` adds 24-bit · `--no-denoise`
  - `--ref <track>` matches a reference (needs `pip install navig-audio[master]`)
  - `--dry-run` prints the chain and the input loudness and writes nothing — run it first.
- `navig audio loudness <files…>` — integrated LUFS, loudness range, true peak.

Notes:
- Report the measured result (LUFS, true peak, duration) from the command's output — the
  numbers are measured on the written file, not assumed.
- The vocal must be a dry take recorded on that beat; `--offset` aligns a take that starts
  later than the beat.
- A loud preset (ritual −9) on a quiet take is fine: the take is levelled before the chain.
