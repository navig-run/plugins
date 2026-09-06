# Changelog — navig-dedupe

## 0.1.1 — 2026-09-03

### Fixed
- **`ndup --help` crashed on a legacy Windows console.** The Typer help contains emoji;
  on a console using an ANSI/OEM code page (cp1251, cp866, cp437) rich cannot encode them and
  click raises `UnicodeEncodeError` while rendering `--help`, before a single line reaches the
  user. Measured against the published 0.1.0 wheel on cp1251: it tracebacked, and the same
  command under `PYTHONUTF8=1` worked. Inside navig this never happens because navig sets the
  console up — standalone nothing does, which is exactly the environment this package promises
  to work in. The CLI now reconfigures its own stdio before the app renders anything.

All notable changes to this package are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Fixed
- **A scan reported its own quarantine back as duplicates.** After the first `--move`,
  every file in the quarantine dir is by construction byte-identical to the original it was
  quarantined *for*. Scanning the parent on a later run therefore surfaced all of them as
  fresh duplicates — telling you to quarantine files that already are, and in the worst case
  nominating the quarantined copy as the keeper and pushing the live original out.
  `_finalize` already stripped the quarantine dir of the *current* run (derived from
  `--move`); it knew nothing about the ones previous runs left behind.

  `files` mode now skips quarantine-shaped directory names by default
  (`DEFAULT_SKIP_DIRS`: `.trash`, `_trash`, `_dupes`, `_duplicates`, `_quarantine`,
  `$recycle.bin`, `system volume information`, …) when they appear *inside* the scanned
  tree. Pointing the root directly at one still scans it, so deduping a trash folder on
  purpose keeps working.

### Added
- **`navig dedupe scan --exclude NAME`** (repeatable) — additional directory names not to
  descend into, on top of the defaults.

### Changed
- The recursive walk prunes with `os.walk` instead of listing everything with `rglob` and
  filtering afterwards, so a skipped directory is never traversed at all. On a multi-TB
  drive that is the difference between minutes of pointless `stat` calls and none.



### Changed
- **`files` mode no longer reads the whole library to find the duplicates.** `hash_dir`
  opened and SHA-256'd every file in the tree; on a 193 GB music library that is 193 GB of
  reading to report a handful of groups. Two exact sieves now run first — file **size**
  (`stat`, no read) and the first **64 KB** — and only what survives both is hashed in
  full. A file at or under 64 KB reuses its head hash instead of being read twice.
  Measured on 29,055 files / 193 GB: **193 GB → 2.1 GB read (93× less)**, producing
  byte-for-byte the same clusters as hashing everything.

  The sieves cannot miss a duplicate: identical files necessarily share a size and a head.
  `hash_dir` now returns hashes only for files that could cluster — the sole consumer is
  `cluster()`, whose output is unchanged.

## 0.1.0 — 2026-07-24

Initial release. The four dedupe engines were extracted out of navig-core
(`navig/media/*_dedupe.py`) into this standalone-first package — the `voice/`→navig-audio
pattern. `navig-core/navig/media/*_dedupe.py` are now thin re-export shims, so the two
never fork and `navig media dedupe-*` keeps working unchanged.

### Added
- **Standalone CLI** — `pip install navig-dedupe` → `navig-dedupe` / `ndup` /
  `python -m navig_dedupe`, no navig required.
- **`navig dedupe`** — the same Typer app mounts inside navig via the `navig.commands`
  entry point; a free `dedupe` module tile + a routing `SKILL.md`.
- **Four engines** — `image` (256-bit dHash + thumbnail pass), `file` (exact SHA-256),
  `audio` (Chromaprint/`fpcalc`), `video` (keyframe dHash). The largest / highest-res
  file per group is always kept.
- **`scan <dir>`** — modes `--images` / `--files` / `--audio` / `--video` / `--all`
  (default: images + files), `--near`, `--recursive`, `--json`. Dry-run by default;
  `--move <dir>` quarantines extras (move, never delete). `-v`/`--verbose` lists every
  duplicate filename per group (not just counts); `--fail-on-dupes` exits non-zero when
  any duplicate is found, so a dry run can gate a CI build.
- **`restore <dir>`** — undo a quarantine. `scan --move` writes a `.dedupe-restore.json`
  manifest; `restore` moves every file back to its original path, leaving any whose
  original path is re-occupied safely in quarantine (`--to` overrides the target root).

### Fixed
- **Cross-mode double-counting** — a byte-identical photo caught by BOTH `--images` and
  `--files` (the default combo) is now counted and quarantined **once**, not once per
  mode. `_finalize` claims each file exactly once across all clusters.
- **Quarantine-inside-root** — when the `--move` dir sits inside the scanned root, a
  repeat `--move` (even `--recursive`) never re-quarantines already-moved files and never
  mistakes a quarantined file for the "keeper" and pushes the live original out.

### Notes
- `audio` needs the `fpcalc` (Chromaprint) binary; `video` needs `ffmpeg`/`ffprobe`.
  Missing binaries skip that mode with a clear message — images/files still work.
- Non-destructive by design: nothing is ever deleted; extras are moved and restorable.
