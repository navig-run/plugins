# Changelog — navig-audio

## Unreleased

### Security (0.5.1)
- **Windows playback no longer runs a file name as PowerShell.** The fallback player pasted the
  path into a shell command string; a file legally named
  `a'); New-Item -ItemType File -Path pwned.txt -Force; #.mp3` executed `New-Item` (verified).
  The script is now fixed text run without a shell, and the path reaches it in an environment
  variable.

### Changed (0.5.1)
- **TTS and STT resolve keys through navig-media-core's `resolve_media_key`**, vault first as
  before, with the same legacy labels. ElevenLabs TTS now also accepts `ELEVEN_API_KEY` (the
  name navig-generate reads), so one env var configures both.
- **The loudness meter, SRT time stamps and durations are navig-media-core's.**
  `navig_audio.mastering.loudness` is an identity alias of `navig_media_core.media.loudness`
  (same `measure`, `Loudness`, `LoudnessError`); `podcast.srt.format_timestamp` and the
  mastering/voice-line duration probes delegate to the shared ones, keeping their contracts
  (0.0 when unknown; `LineError`). The voice-line probe had no timeout — a stuck ffprobe hung
  the render; it is now bounded.
- The Audio Enhance deck routes import the generation service from its owner,
  navig-generate (>= 0.4.0).

### Added
- **DeepFilterNet engine for speech noise** (local `deep-filter.exe`, detected, SHA-256-identified;
  `--set engine=local` opts out). Heavy-noise detection and noise-aware verification fixes.
- **Voice profiles, policy and usage ledger.** `navig_audio.voice.profiles` turns a
  `speech` preset into exactly what the engine is asked to do, and reports the fields the
  provider cannot honour. `navig_audio.voice.usage` keeps paid read-aloud characters per
  day and provider (atomic JSON, 62 days; an unreadable ledger is set aside, never
  overwritten). `catalog.route()` applies the voice policy (auto · free · premium · offline),
  and the catalog carries per-provider prices with their source and date. New
  `navig voice policy | budget | usage` and `navig voice speak --profile`.- **`navig_audio.voice.catalog` — one honest view of the speech engines.** Per provider:
  available here (library installed, key in the vault), cost class (free / free-promo /
  paid), languages, curated fr/ru/en voices, and the controls it really implements.
  `pick_provider()` is the routing rule: Auto means free engines only, and a paid provider
  runs only when it is named. No network call. Backs the Dock's read-aloud and
  `/api/deck/voice/*`.- **`navig audio enhance` + `navig audio enhance-presets` — Audio Enhance.** Measure an
  existing recording, plan only what the measurements justify (Preserve Original / Studio /
  Cinematic / Custom, shipped as `audio-enhance/*` universal presets), render with ffmpeg at
  the source's own rate and layout, verify the result against the original (alignment,
  events, pitch, tone, new content, clipping, true peak), retry safer or reject, and export
  WAV/FLAC/MP3 without ever writing the original. Deck routes for NAVIG OS (upload, analyze,
  waveform, cancellable enhance job → new variant, export) and an `audio-enhance` skill.

- **`navig audio regenerate` — AI regeneration** (voice change, variations, replace a stem) on
  ElevenLabs, consent-gated (`--allow-cloud`), labelled as generated, with provenance and a
  closeness reading. Deck routes `/media/{id}/regenerate` + `/audio/voices`; Studio card.

- **Audio Lab — ten jobs on top of Enhance.** `delivery` (platform loudness specs, `--fix`),
  `clean-video` (dialogue cleanup, picture copied bit for bit, sync measured), `match`
  (reference tone + loudness), `loop` (seamless loopable ambience beds, local or generated),
  restoration presets `audio-enhance/{cassette,vinyl,phone-call,voice-memo}` (new `highpass_hz`
  / `lowpass_hz` params), `podcast-edit` (local Whisper → reviewable cut list → crossfaded
  apply), `stems` (split once on ElevenLabs, remix locally), `similar` (cached sound-alike
  search), `dub` (ElevenLabs Dubbing, sound-only upload, original track kept, drift checked)
  and `score` (music sections cut to the video's scenes, ducked under dialogue).

### Fixed
- **A changed speed, pitch or model was served the old clip.** The TTS cache key held only
  provider, voice, text and style, so `--speed 0.8` (or a profile's pitch / model) replayed
  the clip cached at the old settings. Settings that change the sound are now part of the
  key — only when they differ from the defaults, so every existing cached clip still hits.
- **Masters were 5 ms late and lost their last 5 ms.** `alimiter` delays its output by its
  lookahead unless `latency=1`; the mastering limiter pass now compensates.

## 0.4.1 — 2026-10-15

### Fixed
- **`voice.line.synth_timed(semitones=…)` never overrode the language default.** Both
  branches of the ternary produced `SEMITONES[lang]`, so per-line pitch control silently
  did nothing; the override now wins (cached re-pitch, no re-billing).
- **`navig audio master` validated typed paths against navig's chdir, not yours.** Relative
  `vocal` / `--beat` / `--ref` paths were refused ("does not exist") while sitting in the
  directory they were typed from. They now resolve via `NAVIG_INVOCATION_CWD` (cwd when
  standalone), with the existence check moved into the command body.
## Unreleased

### Added
- **The channel cast is on the voice roster.** `--voice stanislav | maxim | guillaume |
  bella | marcus | callum | roger` now resolve to their ElevenLabs ids (RU/FR narrators and
  the character bench were documented but never registered). An unknown voice *name* now
  fails before any API call, listing the roster, instead of 404ing with an error that blamed
  the key; raw voice ids still pass through, and every existing cache key is unchanged.
- **`navig audio read` — a text read aloud like a book (the bedtime reader).** Cuts the text
  on sentence boundaries into chunks that each know what surrounds them (so intonation
  carries across a seam), groups them into numbered parts of about ten minutes
  (`read-<id>-part-01-of-04.mp3`), and normalises each well below the podcast loudness so a
  loud sentence never wakes you. Pace 0.9 and a steadier delivery by default
  (`audio.read.speed|voice|lang|part_minutes`). `--dry-run` quotes length, parts and credits
  without spending; the quote is *after the clip cache*, so a text already read costs 0; over
  15 000 credits needs `--yes`; a reading the account cannot afford is refused. Text files may be
  UTF-8, UTF-16, cp1251 or cp1252. Engine in `navig_audio.reader`, background policy (one
  reading per chat, confirm, stop, events) in `navig_audio.reading_job` — which the Telegram
  `/read` command and the agent's `read_aloud` tool both drive. A new `bedtime-reader` skill
  carries the routing rules, with English, Russian and French triggers.
- **Loudness normalisation can no longer cut audio unnoticed.** ffmpeg's `loudnorm` was measured
  turning a 1.9 s join with silence gaps into 0.8 s at exit code 0; the reader verifies the
  duration after normalising and falls back to the un-normalised join, with a warning.
- **`navig audio voice-stats`** — the shared voice cache's ledger, read-only:
  mp3/json/raw/semi/tmp counts, total size, **named orphans** (`json_without_mp3`,
  `mp3_without_json`, `raw_without_mp3` — each one a line the renderer cannot
  serve, so its next render re-bills) and oldest/newest age. `--json` carries the
  whole report; a never-used cache answers `exists: false` with exit 0.
- **`navig audio isolate <file>`** — the ElevenLabs voice isolator in the audio
  toolbox beside `speed` / `slowed` (same operation as
  `navig generate eleven isolate`; empty output is refused, never a 0-byte file).
- **ElevenLabs Scribe as an STT provider** — `navig voice transcribe -p elevenlabs`
  (also `speak`-style provider ids on the STT side): word timings + auto-detect
  language, billed against the ElevenLabs subscription; a provider failure is a
  result, not a crash.

### Fixed
- Ruff F541 (f-string without placeholders) in `podcast/draft.py` and E402 (stray
  mid-file `dataclasses.field` import) in `voice/pipeline.py`.

### Added
- **`navig audio voice-line` — one timed narration line, cached by content.** ElevenLabs
  `/with-timestamps` at 192 kbps with the channel's tuned `voice_settings`, word timings
  written beside the mp3, and a deepen pass (per-language pitch drop + shelf) that keeps
  the duration — so the timestamps stay true. The cache key is byte-identical to the
  channel scripts' derivation and the cache lives in the workspace (`vo/kinetic`), so the
  renderer and this verb share it: a re-render never re-bills, and `--semitones`
  re-pitches from the stored raw instead of synthesizing.
- **`navig audio ambience`** — rain/static/city weather, deterministic (seeded by kind),
  written to `<workspace>/sound` so the mix layers the identical file every render.
- **`navig audio spoken`** — display text → the words a voice should speak: RU cases
  ("1 из 5" → «один из пяти», station 13010, genitive/prepositional ordinals), FR years,
  EN digits unchanged. Ported from the channel's `speech.py`.
- **`navig audio bed assign`** — the channel's music: one track per episode, never shared,
  stored in `<workspace>/sound/bed-assign.json` (channel data, not plugin state); mood
  pools and the library root (`beds.json`) travel with the workspace.
- **`navig audio beat grid`** — the bed's cut grid (onset envelope + tempo autocorrelation),
  cached per (file, offset, length) in the workspace's `sound/grids` — the same numbers
  every rendered episode was cut to, measured once.

### Changed
- **The `audio-generation` skill now activates for Russian and French requests, and
  carries the chat-delivery recipe.** Activation is a substring match over
  `activation_keywords`, which were English only — "сгенерируй хоррор музыку" scored 0, so
  the skill never loaded. Russian stems (`музык`, `джингл`, `озвуч`, …) and French terms
  (`musique`, `bruitage`, `voix off`, …) now live in per-language packs
  (`activation_keywords_ru` / `_fr`, also on `audio-mastering`); the skill body now says
  `gen --json` → `chat_send_file` (mp3/m4a arrive as Telegram's player) and no longer
  tells the agent to ask for an output path first.
- **The shared media engine imports now come from `navig-media-core`** (extracted from
  navig-generate): audio edit, the generator registry and the provider catalog moved there;
  beats, tonality and the ElevenLabs client stay in navig-generate. Both packages are now
  declared dependencies. No behaviour change — the public `navig.media.*` paths are untouched.
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
- **Xiaomi MiMo TTS + ASR providers.** `TTSProvider.MIMO` (`mimo-v2.5-tts`, free in
  promo; chat-completions shape, base64 audio in `choices[0].message.audio.data`,
  built-in voices incl. Mia/Chloe/Milo/Dean + voiceclone/voicedesign models) and
  `STTProvider.MIMO` (`mimo-v2.5-asr`, $0.074/h; `input_audio` content part,
  mp3/wav only — other containers converted via ffmpeg with a named error).
  Both audited live 2026-10-13 with a synthesis→transcription round trip.
- **OpenRouter Fish Audio TTS provider** — `TTSProvider.OPENROUTER` posts to OpenRouter's
  OpenAI-compatible `/audio/speech` endpoint (raw mp3 back), default model `fish-audio/s2.1-pro`
  (the `-free` variant 404s "No endpoints found", measured 2026-10-12). `voice` is omitted when
  unset so Fish Audio uses its model-default voice instead of rejecting another provider's voice id.
  Vault key: `navig vault` `openrouter` / `OPENROUTER_API_KEY`.
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
