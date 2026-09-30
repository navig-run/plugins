# Changelog — navig-explore

## Unreleased

### Fixed
- **`rename` left case-only folder renames undone.** On a case-insensitive disk the lowercase
  target "already exists" — it is the old folder — so the files moved in and the folder kept
  its capitals (19 folders on the first real run). A dedicated pass renames them through a temp
  name; `undo` reverses it; `verify` now fails on a name in the wrong letter case.
- **An emptied old folder held open by Windows was dropped silently.** Removal is retried and
  anything still held (an open Explorer window, a media library) is reported as left behind.
- **The rename map rewrote itself.** The map describes the old names on purpose; `apply` no
  longer rewrites the map file it was given.

### Added
- **`explore rename plan | apply | verify | undo` — a whole tree to clean English/Latin names.**
  Folders from a map, files as transliterated slugs, text files tagged with language (and a
  year when the file states one); every reference to a moved path rewritten — including
  relative markdown links from files that themselves moved and JSON-escaped absolute paths —
  with a backup of each rewritten file, an MD5 manifest, a verify that fails on any leftover
  old path, and a full undo. Junctions are never followed.

## 0.2.0 — 2026-09-28

### Added
- **Runs on its own.** `pip install navig-explore` now gives a `navig-explore` command with no
  navig installed: the same app navig mounts as `navig explore`, every subcommand included. It
  no longer depends on navig; what it needed from navig's core (the JSON stores that refuse to
  overwrite what they could not read, the local web server that steps around Windows'
  reserved ports, the console) now comes from `navig-sdk`, which uses navig's own code when
  navig is present. Both doors share one cache: thumbnails made by `navig-explore` are reused
  by `navig explore`, and the reverse.

### Changed
- Advice such as "Run first: … probe" names the command that exists where you are:
  `navig-explore …` on its own, `navig explore …` inside navig.

## 0.1.1 — 2026-09-01

First changelog entry. This package existed before this file did, so earlier
history lives in the monorepo's git log rather than being reconstructed here —
an invented history would be worse than an honest starting point.

### Packaging
- Declares its dependency on `navig` (or deliberately does not, if it runs
  standalone) and ships the full licence text in the wheel.
