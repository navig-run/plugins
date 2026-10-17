# Changelog — navig-media

## Unreleased

### Changed (0.2.0)
- **The suite includes `navig-video`** (it was missing from the bundle and the report), and
  the content row reports `navig content`. Floors raised to the versions of the media
  consolidation (ADR-0002): generate 0.4.0, audio 0.5.1, text 0.5.1, video 0.3.0,
  pipeline 0.7.0. `navig-media --json` now lists seven components.

## 0.1.0 — 2026-10-13

- First release: a metapackage bundling `navig-generate`, `navig-audio`, `navig-text`,
  `navig-pipeline`, `navig-download` and `navig-social`, plus the `navig-media` command
  that reports what of the suite is installed (versions + commands, `--json` for scripts).
- `film` extra adds `navig-browser[drive]` for filming live URLs in clips and reels.
