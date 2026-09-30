# Changelog — navig-calendar

## 0.2.0 — 2026-09-28

### Added
- **Runs on its own.** `pip install navig-calendar` now gives a `navig-calendar` command with no
  navig installed: the same app navig mounts as `navig calendar`. The ICS/CalDAV providers now
  live in this package (navig's `navig.agent.proactive.ics_calendar` is an alias of
  `navig_calendar.ics`), and feeds are fetched through navig-sdk's SSRF guard — the same single
  copy navig uses.
- `list --ics <url>` reads any ICS feed directly (Google's private address works), no config needed.

### Fixed
- **A feed that could not be read printed "No upcoming events" and exited 0.** A blocked,
  unreachable or failing feed is now an error with the reason, exit 1. An unreachable host used
  to end in a traceback (httpx errors are not `OSError`).
- **`icalendar` was declared nowhere**, so ICS worked only where it had been installed by hand.
  It is a dependency now; CalDAV is the `caldav` extra.
- **`sync` printed "✓ Calendar synced" and did nothing.** There is no cache: it now says so.

## 0.1.2 — 2026-09-04

### Fixed
- **Requires `navig>=3.25.0`.** 0.1.1 declared `navig>=3.24.0`, and the published navig 3.24.0
  does not contain modules this package imports — `core/pyproject.toml` had said 3.24.0 for 541
  commits, so the source and the wheel answering to that number were different code. Installing
  with the navig it asked for produced `ModuleNotFoundError`: immediately if the import was at
  module scope, otherwise the moment the feature ran. navig 3.25.0 contains them, and
  `scripts/check_plugin_core_floor.py` now checks every declared floor against the file list of
  the wheel it actually selects.

## 0.1.1 — 2026-09-01

First changelog entry. This package existed before this file did, so earlier
history lives in the monorepo's git log rather than being reconstructed here —
an invented history would be worse than an honest starting point.

### Packaging
- Declares its dependency on `navig` (or deliberately does not, if it runs
  standalone) and ships the full licence text in the wheel.
