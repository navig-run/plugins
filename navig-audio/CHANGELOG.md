# Changelog — navig-audio

## Unreleased

### Changed
- **`beat fit` moved to navig-text** as `navig text lyrics fit` — counting syllables needs no
  audio stack. `beat fit` remains as an alias with the same options; without navig-text
  installed it says how to get it. navig-audio does not depend on navig-text.

### Fixed
- **A single NaN sample silenced a whole demo.** rubberband's pitch shift followed by `vibrato`
  emits one non-finite sample; `loudnorm` spread it over the entire output and LAME aborted
  (`psymodel.c: el >= 0`) — 3 of 30 real demos failed. Every mix (`demo`, `layer`) now scrubs
  non-finite samples to silence before it is normalised.
- **Triplet readings were flagged as off-tempo renders.** Rattles and 6/8 grooves pull the
  detector onto the dotted pulse (a 92 bpm beat read as 61.5, a 150 bpm one as 99.5); a 2/3
  or 3/2 reading now counts as on tempo, noted as `triplet` in the sidecar and INDEX.
- **A layer sheet failed on a render with no quiet intro.** Block specs take fallbacks —
  `block: "build|full:1"` — first that exists wins.

### Added
- **`beat fit --beats DIR`** — suggests the nearest rendered beat for each text, at ½×, 1× or
  2× (a text of short lines on a boom-bap beat is two lines per bar), preferring a straight
  match on a near-tie.
- **`beat analyse --bars` — the arrangement a render actually has.** One loudness value per
  bar, phase-aligned to the downbeat, grouped into `build / full / mid / drop / tail` blocks
  with bar numbers and timecodes; every `beat gen` stores it in the sidecar. Measured on real
  renders: a planned 4-bar intro came back as 8, a planned hook as 15 near-silent bars.
- **`beat fit`** — syllables per lyric line against the bar at a tempo (RU/EN/FR), the lines to
  split, fill or keep, the tempo a text naturally wants; `--all DIR --report FIT.md` for a
  whole library.
- **`beat layer`** — ElevenLabs sound effects (or files) placed on measured bars/blocks with
  per-layer gain and fades, one ffmpeg pass (`adelay`/`afade`/`amix normalize=0`/`loudnorm`),
  effects cached by prompt; the dry beat is never overwritten.
- **`beat demo`** — a 30 s guide vocal: hook lines spoken by stock voices, pitched per
  character (`rubberband`, formants kept or shifted, effect presets), placed on their bars,
  beat side-chain ducked, tagged `GUIDE — NOT FOR RELEASE`. Speech cached per line.

## 0.3.0 — 2026-09-29

### Added
- **Word-level transcription** — `navig_audio.voice.words.transcribe_words(audio)` returns
  Whisper's `verbose_json` shape with each word's start and end: the OpenAI Whisper API
  (`timestamp_granularities=word`) when an OpenAI key resolves, faster-whisper on this machine
  otherwise. navig-pipeline's captions use it, so captions work with no key at all.

### Fixed
- **The network voices could not run from a plain install.** `transcribe -p whisper_api` /
  `-p deepgram` and `speak -p openai` / `-p elevenlabs` all import aiohttp, which this package
  never declared ("aiohttp not installed"); it is a dependency now. (Inside navig it was
  always present, which is how it went unnoticed.)

## 0.2.0 — 2026-09-29

### Added
- **Runs on its own.** `pip install navig-audio` gives a `navig-audio` command with no navig
  installed: `gen`, `check`, `speed`, `slowed`, `beat …`, the podcast commands (`draft`, `plan`,
  `translate`, `render`, `voices`, `clone`, `publish`) and `voice speak|transcribe|list-voices`.
  Inside navig they stay `navig audio …` and `navig voice …`.
- **`navig voice` lives here now.** The `speak` / `transcribe` / `list-voices` commands moved from
  core into `navig_audio.commands.voice`; core mounts them, and without navig-audio each one
  still names the plugin to install.
- `navig_audio.generation.generate_audio` is the public audio backend other plugins call
  (navig-pipeline's narrate stage). `_audio_backend` remains as an alias.

### Changed
- The media engine (audio edit, beats, tonality, the ElevenLabs client) comes from
  navig-generate; paths, files, console and AI from navig-sdk; keys from navig-vault. Depends on
  navig-sdk>=2.14, navig-generate>=0.2.1 and navig-vault>=0.7 instead of navig.
- Podcast `draft` / `translate` use navig-sdk's AI: navig's router inside navig, the user's own
  key or a local Ollama on its own. No AI configured is a one-line error, not a traceback.

### Fixed
- **Offline transcription never worked from a plain install.** `whisper_local` imported
  openai-whisper, which this package never declared, while the faster-whisper it does install
  went unused: every `transcribe -p whisper_local` failed with "whisper not installed". It uses
  faster-whisper first now (openai-whisper still works when that is what is installed), and
  retries on the CPU when faster-whisper picks a GPU whose CUDA libraries are missing
  (`cublas64_12.dll is not found`), which otherwise failed every call on such a machine.
- Hints name the command that exists where you are: `navig-audio …` and `navig-vault add …`
  on their own, `navig audio …` and `navig vault set …` inside navig.

### Added
- **`navig audio beat` — beats to rap on.** `analyse` measures a reference (tempo with a
  *felt* reading when the detector returns double-time, key by Krumhansl profile with the 808's
  own note breaking the relative-major/minor tie, loudness sections, spectral bands) and derives
  a style brief; `styles` lists presets — eight built-in genres plus any YAML passed with
  `--styles`, where a house style may `extends:` a built-in and add to it; `plan` builds the
  provider's composition plan with **bar-exact** section durations (and `--from-api` asks
  ElevenLabs to refine it, which costs no credits); `gen` renders it instrumental, names the file
  `<style>-<bpm>bpm-<key>-NN.mp3`, never overwrites a take, and writes a `.json` sidecar plus an
  `INDEX.md` row with target → **measured** tempo/key so an off-tempo render is flagged instead
  of discovered on the first take. `--dry-run` prints the request and sends nothing.
- Core: `AudioGenerator.generate()` takes `composition_plan`, `seed`, `force_instrumental` and a
  music `model_id` (music_v1 / v2 / v2_5), building exactly one of the two request shapes the API
  accepts; new `AudioGenerator.music_plan()` calls the free `/v1/music/plan` endpoint. New
  `navig.media.tonality` (ffmpeg + numpy, like `beats`) for key / bands / loudness sections.

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

## 0.1.1 — 2026-09-04

### Fixed
- **Requires `navig>=3.25.0`.** 0.1.0 declared `navig>=3.24.0`, and the published navig 3.24.0
  does not contain modules this package imports — `core/pyproject.toml` had said 3.24.0 for 541
  commits, so the source and the wheel answering to that number were different code. Installing
  with the navig it asked for produced `ModuleNotFoundError`: immediately if the import was at
  module scope, otherwise the moment the feature ran. navig 3.25.0 contains them, and
  `scripts/check_plugin_core_floor.py` now checks every declared floor against the file list of
  the wheel it actually selects.

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
