# Changelog — navig-msstore

## 0.7.0 — 2026-09-28

### Added
- **Runs on its own.** `pip install navig-msstore` now gives a `navig-msstore` command with no
  navig installed: the same app navig mounts as `navig mstore`. Credentials, packaging,
  status and the add-on commands work without navig (credentials come from the
  `NAVIG_MSSTORE_*` / `AZURE_*` environment variables when the navig vault is not there).
  `publish` still applies the verified `msstore-publish` block, which needs navig, and now says
  so in one line instead of failing with "navig not found".

## 0.6.0 — 2026-09-01

First changelog entry. This package existed before this file did, so earlier
history lives in the monorepo's git log rather than being reconstructed here —
an invented history would be worse than an honest starting point.

### Packaging
- Declares its dependency on `navig` (or deliberately does not, if it runs
  standalone) and ships the full licence text in the wheel.
