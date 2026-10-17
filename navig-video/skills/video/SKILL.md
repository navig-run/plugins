---
name: video
description: Render a channel's video formats — kinetic episodes, bulletins, motion cards, per-destination encode variants — from its workspace of texts, plates and voice. Use when the user says "render the episode", "make the kinetic", "build the bulletin", "cut a variant for TikTok", "is the workspace ready to render".
---

# video

Render a channel's videos. The channel's media (texts, plates, voice, beds) stays in
its **workspace**; this plugin only carries the rendering code.

## Workspace

Every command takes `--workspace/-C` (env `NAVIG_WORKSPACE`, default
`C:\studio\schema\promo\wave1`). Run `navig video doctor` first — it exits 1 and
names the missing piece (workspace layout, ffmpeg) when a render cannot work.

## Verbs

- `navig video status` — what is wired: workspace, ffmpeg, engines, verbs.
- `navig video doctor` — deep checks; exit 1 = do not attempt a render yet.

Wired today: `kinetic render` and `kinetic plan` (word-timed episodes, parity-
checked against the channel's own renderer), `cut` (renders an EDL — the editable
timeline — into its outputs; see plan 21), and `parity`. The rest (`bulletin`, `format`,
`rollcall`, `montage`, `variant`) arrive as their engines are ported; `status`
computes each verb's wired/planned state from the live command tree — trust that
list, never assume a verb exists.

## Engines

- **voice + beats** come from `navig-audio` (`pip install "navig-video[render]"`).
- **frames/mix/encode** are ffmpeg on PATH (probed at runtime, never a pip dep);
  the frame-pipe and filter primitives come from `navig-media-core`'s media engine.
- Keys (only where a render generates a plate) resolve env-first, then the shared
  vault — never pass a key on the command line.

## Fonts

`kinetic render` loads three faces, never shipped in the repo, from
`%LOCALAPPDATA%\Microsoft\Windows\Fonts`:

- `BebasNeue Bold.ttf` — the slam face (has Cyrillic; Druk does not)
- `JetBrainsMono-Medium.ttf` · `JetBrainsMono-Bold.ttf` — HUD and readout

There is **no fallback**: a missing face makes `render` refuse up front, naming
it (exit 1) instead of dying mid-frames, and it refuses before any voice synth
or network work. `plan` never needs fonts. Byte-parity with the channel's own
renders assumes all three are installed.

## Parity rule

A render verb is not done until it reproduces the channel's existing script output:
same decoded frames (sampled RGB MAE ≤ 1.0/255), duration ±0.1 s, loudness ±0.5 LUFS.
The channel's scripts stay runnable until that parity check passes.
