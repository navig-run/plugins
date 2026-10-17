# Changelog — navig-pipeline

## Unreleased

### Added (0.7.0)
- **`navig content` is the command's canonical name** (and `navig-content` the standalone
  script) — the plugin is content production, not a general workflow engine (ADR-0002).
  `navig pipeline` and `navig-pipeline` keep working unchanged: same app, verbs, flags, exit
  codes and `--json` stdout; `navig pipeline` adds one deprecation line on stderr only (it
  stays listed in `navig --help` for now; a follow-up turns it into a hidden alias).
  The distribution keeps its name; data stays in `~/.navig/pipeline`.

### Fixed (0.6.1)
- **Captions never write a `,1000` millisecond field.** The SRT writer rounded the fraction
  on its own, so 1.9996 s became `00:00:01,1000` (an invalid stamp players drop); it now uses
  navig-media-core's whole-millisecond formatter.
- **A stuck ffprobe/ffmpeg no longer hangs an episode build.** `episode` durations and window
  loudness, and `series` durations, go through navig-media-core's bounded `probe_duration` and
  `loudness.measure` (same readings; `EpisodeError` / -70 LUFS / `None` as before).

## 0.6.0 — 2026-10-16

### Added
- **`navig pipeline episode build <dir…>` — one command from a plan to a ready-to-post pack.**
  narrate → qa (scene + layout) → render → qa (video) → publish → recipe, stopping at the
  first failed stage and printing one row per stage. Paid steps (voicing uncached lines,
  generated video shots) are refused unless `--allow-spend`; nothing is ever uploaded.
  Accepted on the real E07: identical cuts, yuv420p render, clean pack, zero manual steps.
- **Narration moved into the plugin** (`navig_pipeline.episode.narrate`, from the studio's
  `rebuild_episode.py`): verified identical on E04 — same cuts, bed level, gain, karaoke cues
  and shotlist. `episode plan` reconstructs a `rebuild.json` from an episode cut before plans
  existed (lines from `_vo/build_vo.py`, each cut as "after line X, hold H").
- **`localize --narrate` — native-voice localization.** The narration plan is translated,
  voiced in the target language, and the cuts re-placed from that voice into its own voice
  folder, scene, shotlist and `posting.<lang>.json`; karaoke words carry the target voice's
  real timings. Shipped: French E07 (`out-v2-fr/`, 26.3 s, 9.1 LU).
- **`episode draft <article>` — an episode drafted from a campaign article.** Commands must
  resolve in the shipped command manifest AND be literally capturable (no `a | b`, no
  `<placeholder>`); numbers must appear in the article; state-changing captures are flagged;
  hashtags are made TikTok-safe. Writes scene/rebuild/posting + CAPTURE.md. Drafted E08 from
  article 02.
- **`episode stats record|show`** — a series' episodes side by side: what each cut is (hook,
  cuts, words/s, render length) joined with numbers recorded into navig-social's existing
  engagement ledger (snapshots, never summed).
- **`qa` checks copy against footage** — a number in posting copy that no capture or scene
  text shows is an error (`unsupported-number`; E05's "840 extensions" would have failed).
- **Golden stage scenes** (`tests/fixtures/stage/`: E02, E05, E07 as shipped) linted and
  layout-probed in the plugin suite, so a stage change that breaks a layout fails at landing.
  Mutation-checked: removing the caption guard and restoring the 66px caption fails it.

### Fixed
- **A localized pack replaced the original's POSTING.md entry** — blocks were keyed by
  filename and both cuts shared one; keys are now the clip's path relative to POSTING.md,
  and a localized shotlist gets its own title (so its own render filename).
- **`--plan rebuild.fr.json` from the series folder** resolves inside the episode.

## 0.5.0 — 2026-10-15

### Added
- **`navig pipeline qa` — catch a video's design bugs before rendering it.** A scene lint
  (an orphaned karaoke word, a sentence tail stranded on the next screen, a capture-only
  flag such as `--udid` about to be typed on screen, a command that wraps three lines),
  `--layout` (the compiled stage measures each segment alone, settled: a caption running
  into its panel, a box under TikTok's UI strips) and `--video` (1080×1920 · 30 fps ·
  4:2:0 · audio · length). Every rule is a bug that shipped in the first narrated series.
- **`navig pipeline publish <clip>` — a render → a ready-to-post pack.** Spec check, a
  cover from the hook (t=1.6s, the full headline on screen), title/caption/hashtags from
  the episode's `posting.json` or its hook, written beside the clip and optionally upserted
  into a series `POSTING.md` (byte-identical outside its block, line endings preserved).
  `--youtube` runs the real `social upload --dry-run`. Nothing is uploaded; TikTok stays a
  human upload — navig has no TikTok upload API.
- **`navig pipeline recipe export|apply` — an episode's cut as one portable JSON.** Scenes,
  shotlists, the narration plan (`rebuild.json`, or `_vo/build_vo.py` + `beats.json`) and the
  casts, sha256-checked, byte-exact; binary assets listed, not carried; never half-applied;
  a path leaving the episode folder is never written.
- **`navig pipeline localize scene.json --to <lang>` — one capture, many locales.** Every
  string a viewer reads is translated (a reviewed `--strings` file first, the SDK model seam
  for the gaps); terminal output and window bars are real captures and never touched. Cues
  keep their English windows with words spread across them (v1).
- **One karaoke-cue chunker** — `captions.chunk_cues` / `speech_block`. The rules used to
  live in two studio scripts that drifted (one regenerated a scene nobody rendered, and an
  orphaned "files." shipped); verified identical on all six narrated episodes.

### Fixed
- **Every CDP run ended in "I/O operation on closed pipe" tracebacks.** The browser session
  was opened inside one `asyncio.run` and released from another; `clip`, `reel` and `qa` now
  detach it inside the loop that opened it. Zero teardown noise on a real `qa --layout`.
- **A panel pushed down by a tall caption could slide under TikTok's caption bar** — it is
  now shrunk to stay above y=1500 (its rows already scroll the newest line into view).

## 0.4.1 — 2026-10-15

### Fixed
- **A typed command wrapped mid-token.** `.cmdrow` used `word-break: break-all`, so a long
  package name split as `com.samsung.androi` / `d.app.notes`; it now breaks at spaces first
  and only inside a token that cannot fit a line alone.
- **The terminal panel could ride up under a tall caption.** The centre band started at a
  fixed y=440, which a 3-line transcript at the old 66px crossed (seen on E05 — the panel
  edge cut through “reach.”). `fit()` now measures the caption's real bottom (the longest
  cue is pre-rendered so the measurement is the worst case) and starts the panel below it;
  the caption band also drops 66px → 56px.
- **A dotted-quad version number refused a capture as a leak.** `Version 4.4.45.37`
  (Samsung) tripped the IPv4 scanner; a quad preceded by `version|ver|build|release` is
  now classified by context — the same quad without that word is still refused.

## 0.4.0 — 2026-10-14

### Added
- **Karaoke transcript segments (`speech` in a scene).** A segment may carry
  `{cues: [[text, t0, t1]…], words: [[word, s, e]…]}` in section-local seconds and the
  caption band becomes a spoken-word transcript: words land as the narrator says them,
  the newest carries the accent flash, the last phrase holds through silences. Timing is
  driven by the render clock (never wall time), so stepped renders reproduce it exactly —
  and it replaces burned SRT subtitles, which sat in TikTok's own UI zone.
## 0.3.2 — 2026-10-14

### Fixed
- **The terminal panel could reach the burned-subtitle band and TikTok's bottom UI.** The
  centre band ended at y=1560 — past the stage's own 1520 safe limit and straight through
  where a burned SRT sits (1383..1499, measured). Panels now centre in 440..1310, leaving
  ~70px between the verdict line and the subtitle band.
## 0.3.1 — 2026-10-14

### Fixed
- **`navig_pipeline.stage` imported only from a deep path.** The font search indexed a
  fixed `parents[8]`, which exists inside this repo's worktrees but not under the main
  checkout or site-packages — importing the module there raised `IndexError`, breaking
  every `stage`/`clip` render on main while worktree tests stayed green. Ancestors are now
  walked to exhaustion; a shallow install simply finds no repo roots and falls back to the
  CSS font stack. Pinned by a subprocess import from a shallow temp path.
## Unreleased

### Fixed
- **A reel shot with `provider: elevenlabs` and a seed image no longer demands a
  Replicate key** — Flows reads the local file itself; the unconditional
  `upload_image()` (Replicate-only) failed an ElevenLabs-only run before Flows
  was ever asked.

### Changed
- **The ffmpeg media primitives now come from `navig-media-core`** (extracted from
  navig-generate): `audio_edit` / `video_edit` / `frames` / `fx` imports were repointed and
  the package is a declared dependency. No behaviour change.

## 0.3.0 — 2026-10-13

### Added
- **`navig pipeline cast`** — record a real command's output once, at a phone-sized width
  (asciicast v2) through a pseudo-terminal so colour survives. Home dir, username, host and
  adb serials are redacted (whole and truncated forms) and a capture that still shows an IP
  or MAC is refused rather than kept. Prompts are answered after a stall; nothing is fed to
  a command that is merely slow.
- **`navig pipeline stage`** — compile a scene (`hook` / `terminal` / `telegram` / `image` /
  `outro` segments) into one self-contained 1080×1920 page a reel or clip can film with
  `[shot: url="stage:scene.json#N"]`. Fonts found on the machine are embedded at build time,
  so a render never depends on the network. Terminal segments replay a real capture; nothing
  in the stage writes terminal output.
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
