# Changelog — navig-pipeline

## 0.2.2 — 2026-09-30

### Changed
- **Filming a live URL runs without navig**: clips and reels drive your installed Chrome through
  navig-browser (the new `film` extra, also in `all`). Only `schedule` and `telegram-import`
  still need navig.

## 0.2.1 — 2026-09-29

### Changed
- `pip install "navig-pipeline[all]"` now includes navig-social, which runs on its own since
  0.2.0, so every stage of `run` (script, acquire, publish) installs without navig.

## 0.2.0 — 2026-09-29

### Added
- **Runs on its own.** `pip install navig-pipeline` gives a `navig-pipeline` command with no
  navig installed: `status`, `run`, `captions`, `hooks`, `clip`, `reel` (and `schedule` /
  `telegram-import`, which need navig and say so). Inside navig they stay `navig pipeline …`.
- **Captions need no key.** Transcription moved to navig-audio (`transcribe_words`): the OpenAI
  Whisper API when a key resolves, faster-whisper on this machine otherwise. Captions used to
  carry their own Whisper client and stopped at "no OpenAI key".

### Changed
- The media engine comes from navig-generate, narration and transcription from navig-audio
  (the public `generate_audio`, not the private `_audio_backend`), console and paths from
  navig-sdk. Depends on navig-sdk>=2.14, navig-generate>=0.2.1 and navig-audio>=0.3 instead of
  navig; `[all]` adds navig-text and navig-download.
- `clip` and `reel` start navig's browser only when a shot films a live URL. `reel` used to
  launch one for every build, including reels made only of generated shots and stills.
- Hints name the command that exists where you are (`navig-pipeline …`, `navig-audio …`,
  `navig-vault …` on their own).

### Fixed
- **The Telegram-import archive pipeline can no longer hang the import forever or freeze the
  loop.** `_run_pipeline` shelled out to `ingest.py` (transcribe/OCR over a whole batch = minutes)
  with **no timeout**, called **synchronously inside the async `run_import`** — so a wedged pipeline
  hung the caller indefinitely and blocked the daemon/CLI event loop (the exact issue `_import_links`
  already offloads). It now runs via `asyncio.to_thread` and is bounded by a wall-clock timeout
  (default 1h, `NAVIG_ARCHIVE_TIMEOUT` to raise it); a timeout returns non-zero → `pipeline_failed`
  → **nothing is deleted**. Adds telegram_import's first tests.

## 0.1.0 — 2026-07-09

### Fixed (senior review pass)
- **acquire actually downloads.** URL sources are now fetched to a local file via
  `navig-download` (`fetch_file_async`) before transcription — previously the raw URL was
  passed to STT and the stage falsely reported "downloaded".
- **Honest publish status.** The publish stage (and CLI summary) derive success from the
  publish *receipts*, not the request — an all-failed `--live` run now reports `failed` with the
  per-network reasons, not a green "ran".
- **No placeholder posts.** An empty model draft or a `None`/empty transcript is now a reported
  failure (never a live "Update" placeholder or a "ran — 0 chars" success).
- **Scheduler quoting.** `compose_run_command` uses `shlex.quote` so a value with quotes/backslashes
  round-trips through the cron service's `shlex.split` instead of breaking the job.
- **`--kind` typos error** up front instead of silently falling back to the generic prompt.

### Added
- **The content assembly line.** `navig pipeline run` composes the media plugin family into one
  flow — acquire (navig-download) → transcribe (navig-audio) → script (navig-text) → narrate
  (navig-audio audio gen) → publish (navig-social fan-out). This wires `navig audio gen` in as the
  voiceover stage and feeds AI-drafted captions/briefs straight into the fan-out.
- **Graceful, non-silent degradation.** Each stage is soft-detected; a missing plugin drops its
  stage with a reported reason (never a silent gap). `navig pipeline status` shows what's wired.
- **Safe by default.** `run` is a dry-run (drafts the real caption + previews the fan-out, no side
  effects); `--live` publishes. No hard deps on the stage plugins — they're soft-imported at run time.
- **Autopilot — `navig pipeline schedule`.** Registers a recurring `navig pipeline run … --live` via
  the existing cron service (no reinvention), so the assembly line re-drafts fresh content and fans it
  out hands-free on a cadence (`--every "0 9 * * 1"`). `--dry-run` previews the job; manage with
  `navig cron list` / `remove`.
