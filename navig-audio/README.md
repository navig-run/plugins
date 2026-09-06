# navig-audio

NAVIG **Audio** — the voice subsystem (text-to-speech, speech-to-text, wake-word,
streaming pipeline). A first-party navig plugin (free, toggleable), **extracted from core
`navig.voice`**. Phase 3 folds AI **audio generation** in here too.

## What it does

- **TTS / STT** engines (edge-tts, faster-whisper, OpenAI) behind `navig.voice.*`.
- Powers the `navig voice` CLI, gateway Telegram voice notes, agent voice input, and inbox
  audio transcription.

## How it wires (compat shims)

Core keeps thin `navig.voice.*` **compatibility shims** that re-export `navig_audio.voice.*`.
So every existing consumer works unchanged when navig-audio is installed, and degrades
gracefully (ImportError → feature off) when it isn't. The heavy deps
(edge-tts / faster-whisper / openai) live here, not in core — a bare `pip install navig`
stays lean.

## Install

Bundled with a full `navig` install (the `voice`/`audio` extra); standalone:
`navig store install pip:navig-audio`. Requires `navig-core` in the same environment.

## Roadmap (Phase 3)

`navig-audio` joins `navig-image` · `navig-video` · `navig-text` as the media-type plugin
family, each owning its own `generate`. Audio generation + narration (feeding navig-video and
navig-social) land here, coordinated with siblings via core's refs library + event bus.
