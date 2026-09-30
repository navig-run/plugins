# Changelog — navig-generate

## 0.2.1 — 2026-09-29

### Fixed
- **On its own, variants landed under your home folder.** Looking for the project you stand
  in, `navig-generate` stopped at `~/.navig` — navig's global settings, not a project — so
  anywhere under home counted as one "home" space. It now skips the global layer exactly as
  navig does, and a folder with no project `.navig/` above it is its own space.

## 0.2.0 — 2026-09-29

### Added
- **Runs on its own.** `pip install navig-generate` gives a `navig-generate` command with no
  navig installed: `gen`, `list`, `ingest`, `keep`, `reject`, `edit`, `rembg`, `redesign`,
  `process`, `palette`, `contact-sheet`, `license` — the same commands navig mounts on
  `navig generate`.
- **navig's media engine lives here now.** `video_edit`, `fx`, `audio_edit`, `beats`,
  `tonality`, `frames`, the generation service, the refs library, the pixel pipeline, the
  image/video/audio generators and the variant store moved out of core. navig's old module
  paths are identity aliases, and navig depends on this package.
- `gen` failing for a missing key names the environment variables each provider reads (or
  the provider whose key IS set).

### Fixed
- **The ids `list` and `gen` print did not work.** They show 8 characters and say
  "keep/reject <id>", but every command matched the full 32-character id only. Any unique
  prefix works now; an ambiguous one is refused rather than guessed, and `%`/`_` are literal.

## 0.1.2 — 2026-09-01

### Fixed
- **Declares `navig>=3.24.0`.** 0.1.1 declared no dependency on navig at all. Unlike its
  siblings this package imports core only lazily, so `import navig_generate` succeeded on a
  bare install — and then every deck route raised `ModuleNotFoundError` the moment it ran,
  which is the worse failure of the two: it surfaces as a broken endpoint, not a failed
  install. Only `pip install navig[generate]` ever worked.
- Ships the full Apache-2.0 licence text in the wheel.

This release also carries everything listed under *Unreleased* below.

## 0.1.0 — 2026-07-07

### Changed
- **Renamed from `navig-media` → `navig-generate`.** After the social publishing and
  download halves were extracted into `navig-social` + `navig-download`, the only surface
  left was AI media *generation*, so the overloaded "media" name is retired.
  - Package `navig_media` → `navig_generate`; module id `media` → `generate`.
  - The core `navig media` CLI is renamed `navig generate`; **`navig media` still works as
    a deprecated alias** (one-line notice, then forwards) — no command breaks.
  - Deck route prefix `/api/deck/media/*` is unchanged for now (renamed in Phase 3).

### Notes
- The generation engine remains in core (`navig.media.generation_service`); this plugin is
  only its deck front-end.

### Roadmap
- Phase 3 splits `navig-generate` into `navig-video` + `navig-audio`.
