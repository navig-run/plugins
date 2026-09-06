# Changelog — navig-github

## 0.2.5 — 2026-09-04

### Fixed
- **Requires `navig>=3.25.0`.** 0.2.4 declared `navig>=3.24.0`, and the published navig 3.24.0
  does not contain modules this package imports — `core/pyproject.toml` had said 3.24.0 for 541
  commits, so the source and the wheel answering to that number were different code. Installing
  with the navig it asked for produced `ModuleNotFoundError`: immediately if the import was at
  module scope, otherwise the moment the feature ran. navig 3.25.0 contains them, and
  `scripts/check_plugin_core_floor.py` now checks every declared floor against the file list of
  the wheel it actually selects.

## 0.2.4 — 2026-07-20

### Changed
- **Finished the storage debrand** started in 0.2.2–0.2.3: the four remaining stores that
  still used the placeholder `~/.config/the engine` / `.the engine_*` paths now use
  `~/.config/navig-github` / `.navig-github_*`, so all of the plugin's storage lives in
  one place instead of split across two dirs:
  - notifications config → `~/.config/navig-github/.navig-github_notifications.json`
  - custom templates → `~/.config/navig-github/templates.json`
  - backup-diff snapshot → `.navig-github_snapshot.json`
  - integrity checksums → `.navig-github_checksums`
  Each falls back to reading the old location, so existing config/templates/baselines are
  not lost on upgrade. Template default author `the engine` → `navig-github`.
- Scrubbed the remaining user-facing `the engine` placeholder from `--help` text, the
  notifications-not-configured hint, and `owner/repo` examples (`miztizm/the engine`, an
  invalid repo name with a space, → `miztizm/navig-github`).
- Tests for the notifications/templates/diff legacy-location fallbacks.

## 0.2.3 — 2026-07-20

### Fixed
- **Incremental backup state is written atomically.** A torn `.the engine_state.json`
  read as corrupt on the next run and silently forced a full (non-incremental) re-backup;
  the write now goes through the shared `atomic_write_json` (temp + fsync + replace).

### Changed
- Debranded the two remaining `~/.config/the engine` storage paths to match the profile
  store: the scheduler's default dir → `~/.config/navig-github`, and the incremental
  state file → `.navig-github_state.json`. Both adopt data from the old location once,
  non-destructively (the scheduler copies `schedules.json`; the state file is read from
  the legacy name when the new one is absent).
- Tests for both stores (`tests/test_incremental.py`, `tests/test_scheduler_migration.py`).

## 0.2.2 — 2026-07-19

### Fixed
- **Profile store no longer silently wipes saved profiles.** `save`/`delete` reloaded
  `profiles.yaml` before writing, but a transient file lock or a corrupt read returned
  *empty* — so the write dropped every other saved profile. Reads on the write path are
  now strict: a bad read raises and aborts the write instead of destroying siblings.
- **Atomic profile writes.** `profiles.yaml` is written via a sibling temp + `os.replace`
  (temp cleaned up on failure), so a torn write can't corrupt the store and trigger the
  wipe-on-next-save above.

### Changed
- Default profile directory renamed `~/.config/the engine` → `~/.config/navig-github`
  (the old name was a placeholder with a space in the path). Profiles saved under the old
  path are adopted once, non-destructively, on first run.
- First test suite (`tests/test_config.py`, 6 tests).

## 0.2.1 — 2026-07-06

### Changed
- **Engine bundled in-package** — the GitHub engine is now vendored at
  `navig_github/engine/` (0.11.0) and mounted in-process; there is no separate
  engine folder and no external engine pip dependency.

### Fixed
- `navig github backup` wrapper updated to the engine's 0.11 flags: concurrency
  is `--max-workers` (was `--workers`) and `user`/`org` no longer take `--yes`.
  Verified live against the GitHub API (vault token flowing in-process).

## 0.2.0 — 2026-07-06

### Added
- **Full engine mount** — the GitHub engine is bundled in-package and every
  command is mounted **in-process** as a `navig github` subcommand (~45
  commands): backups (`user`, `org`, `repo`, `starred`, `watched`, `gists`),
  exports (`issues`, `pulls`, `releases`, `wiki`, `labels`, `milestones`,
  `workflows`, `attachments`, `discussions`, `followers`, `webhooks`,
  `secrets`, `profile`, `projects`), integrity & analytics (`verify`, `diff`,
  `snapshot`, `analytics`, `analytics-history`), restore (`restore-issues`,
  `restore-releases`, `restore-labels`, `restore-milestones`), automation
  (`schedule-*`, `notify-*`, `config-*`, `template*`), and remote-destructive
  ops (`delete`, `transfer` — the engine's own confirmation gates apply).
  `navig github --help` groups everything into panels.
- **Vault wiring for the whole surface** — a group callback resolves the
  token (vault `github_token` → `GITHUB_TOKEN` env → `~/.navig/config.yaml`)
  and injects it into `GITHUB_TOKEN` before any subcommand runs; every
  engine command reads it via its `envvar`, so all ~45 commands
  authenticate from the navig vault with zero per-command glue.

### Changed
- The GitHub engine moved from the optional `[engine]` extra to a bundled
  in-package dependency (the extra remains as a no-op alias for back-compat).
- Curated wrappers (`backup`, `search`, `clone`) now dispatch into the engine
  **in-process** instead of via subprocess; `clone` keeps its plain-git
  fallback when no token is configured.
- Curated command names shadow the engine's versions where navig's flags are
  friendlier (`search`, `clone`); `token` and `status` remain navig-only.
