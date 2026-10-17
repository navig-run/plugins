# Changelog — navig-media-core

## Unreleased

### Fixed (0.2.0)
- **ASS caption times no longer print `:60.00`.** `fx` formatted seconds as `05.2f`, so
  59.996 s became `0:00:60.00`; times are now computed in whole centiseconds.

### Changed (0.2.0)
- **`resolve_media_key` is the one media key resolver** — generation, TTS and STT all call
  it. New keyword-only `prefer="env"|"vault"` (each caller keeps its order: generation
  env-first, the voice engines vault-first), `labels=` (legacy vault secret labels, read only
  when passed) and `caller=` (the vault access log). Blank values never count; a locked vault
  falls through to the other source instead of raising.
- **One owner for the ffmpeg primitives every media package used to copy** (ADR-0002):
  `media.ffmpeg.ffmpeg_available()` (was defined three times here), `media.loudness`
  (`measure()` — the `loudnorm` meter, moved from navig-audio, now with an optional
  `start`/`duration` window — and `integrated_lufs()`, the `ebur128` Summary reading the video
  mix and parity checks gate on), and `media.subtitles` (`srt_timestamp`, `ass_timestamp`).
  Every reading runs with a timeout.
- **The library no longer imports navig-generate** (ADR-0002). The generation service moved
  to its owner, `navig_generate.media.generation_service`; `navig_media_core.media.
  generation_service` stays as an identity alias (same module object, so patches still
  reach it). `active_working_dir` now lives in `navig_media_core._compat`. A layering test
  (`tests/test_layering.py`) fails on any new import of a media plugin.
- **One owner per generator modality.** `register_generator(modality, backend, *, owner=None,
  replace=False)` records the owner (default: the backend's top-level package). The same
  owner re-registering replaces its backend; a different owner is refused with a warning
  naming both, so the result no longer depends on import order. New:
  `registered_generators()`, `unregister_generator()`. Existing two-argument calls work.

### Added
- **`import_audio()` and `persist_derivative()` — audio in, processed audio beside it.**
  `import_audio` brings a dropped audio file into the library as its own variant (the copy
  is staged; the file stays where it was). `persist_derivative` is the public form of what
  `isolate()` does: a processed file becomes a NEW variant in the source's group with
  `derived_from` + `op`, the source untouched. Audio Enhance (navig-audio) uses both.
- **Presets reach the generator.** `generation_service.generate(presets=[...], preset_values=,
  preset_overrides=)` resolves universal presets (navig core `navig.presets`): the style clause
  goes after the user's prompt, text presets arrive as the backend's `preset_system`, explicit
  request fields beat preset settings, and the applied presets (id, version, layer) are stored in
  each variant's context. `apply_presets()` is the side-effect-free resolver the deck route uses
  to reject a bad preset with a 400 before a job starts.

## 0.1.1 — 2026-10-15

### Fixed
- **`from_frames` wrote full-range `yuvj420p`, not the `yuv420p` it promises.** Its frames
  are JPEG screenshots (full range), and `format=yuv420p` alone does not convert the range,
  so libx264 kept the deprecated `j` variant and every stage render inherited it through
  stream-copy joins. The filter now converts with `scale=out_range=tv` first; output is
  limited-range `yuv420p` (pinned by `tests/test_from_frames_range.py`).

### Added
- **`video_edit.excerpt` + `video_edit.master` — the video twins** (plan-21 B0).
  `excerpt` cuts `[start, end)` with BOTH streams, frame-accurate (re-encode, not
  keyframe-drifting stream copy), optional `maxrate` cap for platform deliverables;
  `master` loudness-normalizes by stream-copying the picture and filtering only the
  sound, and refuses a file with no audio up front. Both exist because
  `audio_edit.excerpt` writes WAV and `audio_edit.normalize` writes audio-only output
  by design — correct alone, a trap the moment they are handed a finished cut (the
  first demo-01 "renders" were audio-only mp4s). Contract tested in
  `core/tests/media/test_video_edit.py`.
- **Provider catalog: ElevenLabs everywhere it actually applies.** New entries for
  Flows **video** (Veo 3.1, Seedance) and Flows **image** (gpt-image, Gemini image,
  Seedream) — both marked Pro-plan-gated (a creator key gets `402 paid_plan_required`,
  measured live 2026-10-14) — and the audio entry's note now lists the
  `navig generate eleven …` tools. The audio `models` list stays exactly
  `music | sfx | tts`: it feeds a `--kind` picker, and only kinds `AudioKind`
  accepts may appear there (a tools entry would make the picker raise).
- **Audio ops on a variant** — `generation_service.isolate()` (voice isolator → a
  new audio variant beside the source) and `generation_service.transcribe()`
  (ElevenLabs Scribe → text, no variant), the engine behind the two new
  `/api/deck/media/{id}/{isolate,transcribe}` routes.
- **Preflight before a render** — `generate()` now runs one free `subscription()`
  GET when ElevenLabs will serve the request and returns a `preflight` list on the
  result: exhausted/nearly-exhausted credits, and the Pro-plan gate for Flows
  video/image on a sub-Pro key. Probe failures never block a generation, and a
  provider that is not ElevenLabs is never probed.

## 0.1.0 — 2026-10-13

### Added
- **Extracted from `navig-generate`** — the shared half of the media engine now lives here:
  `media/types` (MediaModality + generator registry), `media/generation_service`,
  `media/refs_library`, `media/context_builder`, `generated_media` (the SQLite variant
  store), `tools/media_providers` (provider catalog + key resolution), and the ffmpeg
  primitives `media/audio_edit`, `media/video_edit`, `media/frames`, `media/fx`.
- **No behaviour change.** navig's public paths (`navig.media.*`, `navig.store.generated_media`,
  `navig.tools.media_providers`) are unchanged — the core shims alias this package now instead
  of `navig_generate`. The module objects are still shared singletons, so dotted-path patches
  in tests keep working.
- A library, not a program: no CLI, no entry points, and it imports without navig installed.
