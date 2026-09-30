---
name: dedupe-ops
description: Find and clean up duplicate files — photos, videos, audio, or any file type — in a folder. Use when the user wants to find duplicate/near-duplicate images, remove re-saved or resized photo copies, dedupe a downloads/media folder, or reclaim disk space from duplicates.
activation_keywords: [dedupe, duplicate, duplicates, "duplicate photos", "duplicate files"]
metadata:
  version: 1.0.0
  toolsAllowed:
    - bash_exec
---

# Dedupe Operations

This capability is provided by the **navig-dedupe** plugin. Use `navig dedupe scan
<folder>` to find and quarantine duplicates. It is **non-destructive** — extras are
*moved* to a quarantine dir, never deleted.

- Dry run first (default): `navig dedupe scan <folder>` reports groups without touching files.
- Quarantine the extras: `navig dedupe scan <folder> --move <quarantine-dir>`.
- Undo a quarantine: `navig dedupe restore <quarantine-dir>` moves every file back (nothing is ever deleted). Safe to offer this after any `--move`.
- Modes: `--images` (perceptual: re-saved/resized copies), `--files` (exact SHA-256, any type),
  `--audio` (acoustic fingerprint, needs `fpcalc`), `--video` (keyframe hash, needs `ffmpeg`),
  `--all` (all four). Default with no flags = images + files.
- `--near` loosens image matching to catch resized/re-encoded copies, not just exact re-saves.
- `--json` emits machine-readable output for scripts/agents.

Notes:
- The largest / highest-resolution file in each group is always the one kept.
- Always dry-run and show the user the groups before `--move`; confirm before quarantining.
- `--audio` needs the `fpcalc` (Chromaprint) binary; `--video` needs `ffmpeg`/`ffprobe`. If a
  binary is missing, that mode is skipped with a clear message — images/files still work.
