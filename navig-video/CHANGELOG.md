# Changelog — navig-video

## Unreleased

### Changed (0.3.0)
- **Loudness and durations use navig-media-core's shared meter** (ADR-0002): `parity` and
  the kinetic mix read integrated loudness through `media.loudness.integrated_lufs` (the same
  `ebur128` Summary parse) and durations through `audio_edit.probe_duration`. The mix's
  probes had no timeout; a stuck ffmpeg/ffprobe hung the render. navig-media-core (>= 0.2.0,
  light) is now a base dependency, since `parity` is a base command.

## 0.2.2 — 2026-10-14

### Added
- **`navig video cut <edl>` — the EDL renderer.** Plan 21's editable timeline as a
  command: sources[] + outputs[] of `{src, t0, t1}` segments in, rendered outputs out —
  multi-take assemblies hard-cut, audio mastered per output's target, captions burned
  when the EDL says so. Non-destructive (sources read-only, intermediates in `_cut/`),
  existing outputs kept unless `--force`, usage problems exit 2 and ffmpeg failures
  exit 1. Uses the media-core `excerpt`/`master` twins (render extra).

## 0.2.1 — 2026-10-14

### Changed
- **`kinetic render` refuses up front when a font is missing.** The frame engine has no
  fallback, so a missing `BebasNeue Bold` / `JetBrainsMono-*` face used to surface as a
  traceback mid-frames (after voice synth had already run); now `missing_fonts()` is
  checked right after the workspace check — before bind, before `_vals`, before any
  synth — and the exit names the faces and the install directory (exit 1). `plan` never
  needs fonts. SKILL.md documents the font contract.

## 0.2.0 — 2026-10-14

### Added
- **`navig video kinetic render|plan` — the kinetic episode engine.** A monologue cut word
  by word the way the narrator says it: timing from ElevenLabs word timestamps (via
  `navig audio voice-line`'s shared cache), cuts snapped to the bed's grid, plates that
  move, the HUD/end-card/loading-bar set, VOICE_FX/ambience direction and the −14 LUFS
  master gate — ported from the channel workspace's `kinetic.py` at **render parity**:
  stills byte-identical, duration and frame count exact, decoded audio bit-exact, video
  within the x264 codec floor. `plan` dumps the timeline (beat times, words, plates)
  without rendering. Live numbers come from the workspace's own board fetch; `--still`
  writes one frame; `--stills` forces still plates.

## 0.1.0 — 2026-10-13

### Added
- Skeleton: `navig video status|doctor` — honest wired/planned verb table, workspace layout,
  ffmpeg and engine checks (exit 1 when a required piece is missing), entry points,
  standalone mode, SKILL.md.
