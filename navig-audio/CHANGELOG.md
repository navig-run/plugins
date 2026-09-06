# Changelog — navig-audio

## 0.1.1 — 2026-09-04

### Fixed
- **Requires `navig>=3.25.0`.** 0.1.0 declared `navig>=3.24.0`, and the published navig 3.24.0
  does not contain modules this package imports — `core/pyproject.toml` had said 3.24.0 for 541
  commits, so the source and the wheel answering to that number were different code. Installing
  with the navig it asked for produced `ModuleNotFoundError`: immediately if the import was at
  module scope, otherwise the moment the feature ran. navig 3.25.0 contains them, and
  `scripts/check_plugin_core_floor.py` now checks every declared floor against the file list of
  the wheel it actually selects.

## Unreleased

### Fixed
- **Docs named `navig generate --modality audio`, which exits 2.** `--modality` is an option of
  the `gen` subcommand, not of the `generate` group: the runnable form is
  `navig generate gen --modality audio`.
- **A pinned `user.language` no longer switches transcription off.** The preference is written by
  a human, so it holds a *name* — "Russian" — and every STT provider wants an ISO code: Deepgram
  and the Whisper API reject a name, local Whisper is handed it verbatim. `STT.transcribe`
  normalised `auto`/`detect` but passed a name straight through, so `user.language: Russian` made
  every transcription fail and each surface reported "no speech detected" — a pinned language was
  strictly *worse* than pinning nothing, the opposite of what a preference is for. It now converts
  through `navig.core.language.language_code`, and an unrecognised name degrades to detect rather
  than to a value the provider will refuse. (This is the second of navig's two STT entry points —
  the video-transcript path; `navig.agent.voice_input` is the other, fixed in core.)
- **A poisoned TTS cache entry is no longer served forever.** `_get_cached` returned any
  existing `cache_dir/<key>.mp3` on existence alone — so a **0-byte** file (a 200-but-empty
  API response wrote empty bytes and still reported success, or a crash mid-write) was served
  as a valid cache hit permanently: silence/corrupt audio, never re-synthesized. Now the cache
  read requires a non-empty file (and deletes an empty one so it re-synthesizes), and
  `synthesize` treats an empty synth result as a provider failure instead of caching it.
- **Audio playback no longer orphans a player process on timeout.** Each player
  (`afplay`/`aplay`/`mpv`/`ffplay`/the PowerShell MediaPlayer) was awaited with
  `asyncio.wait_for(proc.wait())`, but `wait_for` cancels only the coroutine — a hung or
  looping player kept running orphaned. A shared `_wait_or_kill` helper now `proc.kill()`s
  the child on timeout.

### Added
- **Audio facet of the shared generation engine (Phase 3.1).** navig-audio now registers an
  `AUDIO` backend into core's generator registry (`navig.media.types.register_generator`), so
  `navig generate --modality audio` and the deck `/api/deck/media` route dispatch through this
  plugin when installed — and fall back to core's built-in backend when it isn't. The provider
  transport stays in core (`navig.tools.audio_generation`); the plugin owns the wiring + verb.
- **`navig audio` CLI** — `gen` (music / SFX / TTS via ElevenLabs, with `--count` batch support
  that writes each clip under a unique name) and `check` (is a provider key configured, no secret
  printed). Voice — speak / transcribe / wake-word — stays under `navig voice`.

## 0.1.0 — 2026-07-07

### Changed
- **Extracted the voice subsystem from core `navig.voice`.** The engines (TTS/STT/wake-word/
  pipeline) + heavy deps (edge-tts, faster-whisper, openai) now live here; core keeps thin
  `navig.voice.*` compatibility shims re-exporting `navig_audio.voice.*`. Every consumer
  (the `navig voice` CLI, gateway Telegram voice, agent voice input, inbox transcription)
  works unchanged when installed and degrades gracefully when absent. Bare `pip install navig`
  is leaner (no ML voice deps in the base).

### Roadmap
- Phase 3: fold AI audio generation in here; join navig-image/video/text as the media-type
  plugin family.
