# navig-generate

NAVIG **Generate** — AI media generation (image / video / audio). A first-party navig
plugin (free, toggleable). **Renamed from `navig-media`** — the overloaded "media" name
is deprecated.

## What it does

- Mounts the AI media-generation deck surface (`/api/deck/media/*`): generate, reroll,
  edit, remove-background, redesign, contact-sheet, palette, history.
- The generation **engine** lives in core (`navig.media.generation_service`); this plugin
  is the deck front-end for it.

## CLI

The `navig generate` command (analyse a video → briefing, and generate images/video/audio)
is a **core built-in** — this plugin registers no CLI. `navig media` remains as a
**deprecated alias** of `navig generate`.

## Install

Bundled with a full `navig` install (the `generate` extra); standalone:
`navig store install pip:navig-generate`. Requires `navig-core` in the same environment.

## Roadmap

Phase 3 splits this into **`navig-video`** + **`navig-audio`** (decoupled video/audio
generation that communicate via core's refs library + event bus).
