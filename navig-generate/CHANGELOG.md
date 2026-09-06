# Changelog — navig-generate

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
