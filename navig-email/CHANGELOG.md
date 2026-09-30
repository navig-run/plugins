# Changelog — navig-email

## 0.3.0 — 2026-09-29

(0.2.0 was never published; its changes ship in this release.)

### Added
- **Runs on its own.** `pip install navig-email` now gives a `navig-email` command with no navig
  installed: the same Gmail mailroom navig mounts as `navig email`. Gmail comes through its
  own IMAP backend with an **app password** (Google Account → Security → App passwords), kept
  in the shared encrypted vault: `navig-email imap connect you@gmail.com`. Gmail's IMAP speaks
  Gmail — the same search syntax (`X-GM-RAW`), labels (`X-GM-LABELS`) and message/thread ids —
  so list, search, read, labels, tag, rules, watch, stats, replied and send all work unchanged;
  sending uses SMTP with the same app password. Inside navig, a linked OAuth Gmail is still used
  first, and a navig user can now choose an app password instead of OAuth.
- `imap connect | status | disconnect`. `connect` signs in BEFORE saving anything, so a wrong
  password is never stored; `status` never shows the password.

### Changed
- AI summaries (`digest --llm`) and follow-up drafts still go through navig's privacy guard
  (it refuses cloud models for private mail unless allowed) and say they need navig on their
  own — they never fall back to "whatever AI is configured" with your mail. Telegram alerts
  from `watch` need navig's bot; on their own the alerts are kept for a pass that can send them.
- Hints name the command that exists where you are (`navig-email …` on its own).

### Fixed
- Without navig, a space's `config.yaml` and `rules.yaml` were read through navig inside a broad
  `except` — silently empty. They are read on their own now.

## 0.1.1 — 2026-09-04

### Fixed
- **Requires `navig>=3.25.0`.** 0.1.0 declared `navig>=3.24.0`, and the published navig 3.24.0
  does not contain modules this package imports — `core/pyproject.toml` had said 3.24.0 for 541
  commits, so the source and the wheel answering to that number were different code. Installing
  with the navig it asked for produced `ModuleNotFoundError`: immediately if the import was at
  module scope, otherwise the moment the feature ran. navig 3.25.0 contains them, and
  `scripts/check_plugin_core_floor.py` now checks every declared floor against the file list of
  the wheel it actually selects.

## 0.1.0 — 2026-09-01

First changelog entry. This package existed before this file did, so earlier
history lives in the monorepo's git log rather than being reconstructed here —
an invented history would be worse than an honest starting point.

### Packaging
- Declares its dependency on `navig` (or deliberately does not, if it runs
  standalone) and ships the full licence text in the wheel.
