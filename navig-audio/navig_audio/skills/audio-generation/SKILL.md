---
name: audio-generation
description: Generate music, sound effects, or speech (text-to-speech) from a text prompt. Use when the user wants to create or generate audio, music, a sound effect, a jingle, or spoken audio / a voiceover (TTS).
activation_keywords: [music, jingle, voiceover, "sound effect", "text to speech"]
metadata:
  version: 1.0.0
  toolsAllowed:
    - bash_exec
---

# AI Audio Generation

This capability is provided by the **navig-audio** plugin. Generate audio from a
prompt with `navig audio gen`:

- `navig audio gen "<prompt>" --kind music` — music from a description.
- `navig audio gen "<prompt>" --kind sfx` — a sound effect.
- `navig audio gen "<text>" --kind tts --voice <id>` — text-to-speech / voiceover.
- `navig audio check` — confirm an audio provider key is configured.

Run `navig audio gen --help` for duration, count, and voice options.

Notes:
- This is audio *generation*. Speech recognition / transcription and wake-word
  live under `navig voice` — use that for turning speech into text.
- Confirm the output path with the user before writing files.
