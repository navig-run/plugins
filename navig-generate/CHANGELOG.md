# Changelog — navig-generate

## 0.4.1 — 2026-10-17
### Security (0.4.0)
- **Deck media routes refuse paths that are not plain local paths.** `ingest`, `palette` and
  an edit's `mask_path` took any path from the request; a UNC path (`\\host\share`) makes
  Windows send the user's NTLM credentials to that host, and the mask is uploaded to the image
  provider. Network paths, URLs and missing paths are now a 400, and a mask must be an
  existing local image file. Drives the user mapped themselves still work.

### Fixed (0.4.0)
- **The deck media routes no longer block the gateway's event loop.** keep, reject, process,
  license, ingest, contact sheet, palette, history, context, the file lookup and reference
  uploads ran SQLite, Pillow and disk work on the loop thread; they now run in a worker thread
  (the store keeps one connection per thread).

### Changed (0.4.0)
- ElevenLabs generation also accepts `XI_API_KEY` (the name navig-audio's TTS reads).
- **The generation service is now this package's own** —
  `navig_generate.media.generation_service`, beside the provider clients it drives
  (ADR-0002). The old paths (`navig_media_core.media.generation_service`,
  `navig.media.generation_service`) are identity aliases. Requires navig-media-core 0.2.0.

### Added
- **`AudioGenerator.dub_start` / `dub_status` / `dub_download`** — the ElevenLabs Dubbing API
  (same voices, translated, timing kept), used by `navig audio dub`.
- **`/api/deck/media/generate` accepts `preset_ids`, `preset_values`, `preset_overrides`.** A
  bad preset id or setting is a 400 up front, not a failed background job. `kind` is no longer
  forced to `music` when omitted, so a text or audio preset can supply it.
- **`navig generate statement` — the channel's statement overlay, ported**: Druk
  carries the line, Fira Mono the readout, corner brackets frame it. Spec JSON in,
  1080x1920 RGBA PNG out; `*red word*` segments alternate the two word colours;
  `--workspace` resolves the mark (`<ws>/mark.png`), `--mark` overrides it.
  **Byte-parity with the channel's original renderer** — 6 specs (HUD, full
  statement, and the bisection cases) all SHA-256 identical, cross-checked with
  `navig video parity`. Media never enters the repo: fonts are the machine's,
  the mark stays in the workspace.
- **Statement font precheck**: `missing_fonts()` names any of the five faces that will
  not load. Everything but Inter-SemiBold falls back silently today — now the CLI warns
  (parity lost) and reports `"font_warnings"` in `--json`; Inter-SemiBold itself missing
  is fatal (it IS the fallback) and refuses with exit 1 instead of crashing mid-render.
- **ElevenLabs beyond audio generation — the rest of the subscription, wired:**
  - **`navig generate eleven …` command group**: `subscription` (plan/credits/cloning),
    `history` + `fetch` (the generation library, free reads), `voices` (public voice-library
    search, `--mine` for your own), and the audio tools `isolate` (voice isolator),
    `change-voice` (speech-to-speech), `stems` (2- or 6-stem zip) and `transcribe`
    (Scribe, word timings via `--json`). Every command offers `--json` where output is
    machine-readable.
  - **ElevenLabs Flows video provider** (`--provider elevenlabs`): Veo 3.1 / ByteDance
    Seedance via submit → poll → signed URL, seed image inlined (a caller-supplied URL
    seed is fetched through the SDK's SSRF guard). Plan-gated: a creator-tier key gets
    the API's `402 paid_plan_required` message verbatim, never a bare status code.
  - **ElevenLabs Flows image provider** (`--provider elevenlabs`): gpt-image / Gemini /
    Seedream models; `ImageSize` → `aspect_ratio` + `resolution` mapped on the LONG edge
    so a requested size is never silently answered smaller.
  - New `tools/media_library.py`: read-only history / voice-library / asset browsing
    (`MediaLibrary`) — no credits consumed by reading.
- `core` mounts `generate_app`'s **sub-groups** too (`registered_groups`), so
  `navig generate eleven …` appears under navig exactly as `navig-generate eleven …`
  does standalone.
- **Deck routes for the ElevenLabs audio tools**: `POST /api/deck/media/{id}/isolate`
  (voice isolator → a NEW audio variant in the same group) and
  `POST /api/deck/media/{id}/transcribe` (Scribe text out, no variant) — both gated
  on the `generate` module, driven from the OS Generate app's audio cards.

### Fixed
- **`navig generate statement` draws Cyrillic.** Druk Medium has no Cyrillic glyphs, so a Russian
  headline rendered *blank* (measured: `268` = 466k px of ink, `ГОРОДОВ` = 0) while digits and
  punctuation survived. The face is now chosen by **glyph coverage** — Druk, then Bebas Neue Bold
  (the channel's own kinetic face for exactly this), then Inter — and the sub-line / tag fall back to
  Inter the same way. Latin and French still render through Druk, **byte-identical**: six specs
  (HUD, French accents, digits, punctuation, the original bisection cases) SHA-256 match the
  channel's original renderer before and after.
- **`eleven history`'s closing hint named a flag that does not exist** — it advised
  `eleven fetch <id> --json`; the ids come from `eleven history --json`.
- **`eleven voices --mine` ignored `--search` / `--language` / `--limit`** — the
  account's own voices now get the same filters as the shared-library search.
- **`eleven isolate` / `change-voice` / `stems` could write a 0-byte "success"** —
  all three now go through the same empty-output refusal as `eleven fetch`.
- **Flows image batches shared one poll deadline and one seed** — a late batch
  member could time out and discard already-charged images, and `seed=42 n=3`
  billed one deterministic image three times; the deadline is per job and the seed
  offsets per iteration.
- The image driver's docstring named a checker (`_check`) this module does not
  have (`_image_check`).

### Changed
- **The shared half of the media engine moved to the new `navig-media-core` package**: the
  media types + generator registry, the generation service, the refs library, the context
  builder, the `generated_media` store, the provider catalog (`tools/media_providers`) and
  the ffmpeg primitives (audio edit, video edit, frames, fx). This package keeps the
  image/video/audio generator clients, beats, tonality, the pixel pipeline, the deck routes
  and the CLI, and now depends on `navig-media-core`. navig's public paths
  (`navig.media.*`, `navig.store.generated_media`, `navig.tools.media_providers`) are
  unchanged — the same modules, re-homed.

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
