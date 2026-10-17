---
name: audio-generation
description: Generate music, sound effects, or speech (text-to-speech) from a text prompt. Use when the user wants to create or generate audio, music, a sound effect, a jingle, or spoken audio / a voiceover (TTS).
activation_keywords: [music, jingle, voiceover, "sound effect", "text to speech"]
# Trigger vocabulary, not copy: "сгенерируй хоррор музыку" scored 0 against an English-only
# list, so the skill — and its delivery recipe — never loaded. Entries are word-start stems
# (музык → музыка/музыку/музыки); see core/navig/agent/skills_context.py for the rules.
activation_keywords_ru: [музык, джингл, саундтрек, озвуч, "звуковой эффект", "звуковые эффекты"]
activation_keywords_fr: [musique, bruitage, "voix off", "synthese vocale", "effet sonore", "effets sonores"]
metadata:
  version: 1.0.0
  toolsAllowed:
    - bash_exec
    - chat_send_file
---

# AI Audio Generation

This capability is provided by the **navig-audio** plugin. Generate audio from a
prompt with `navig audio gen`:

- `navig audio gen "<prompt>" --kind music` — music from a description.
- `navig audio gen "<prompt>" --kind sfx` — a sound effect.
- `navig audio gen "<text>" --kind tts --voice <id>` — text-to-speech / voiceover.
- `navig audio check` — confirm an audio provider key is configured.

Run `navig audio gen --help` for duration, count, and voice options.

## From a chat (Telegram, deck) — generate, then deliver

When the request comes from a chat, the operator wants to HEAR the result there,
not be told a path on their disk:

1. Run `navig audio gen "<prompt>" --kind music|sfx|tts --json`. The output is a
   JSON list; each item's `local_path` is the file. An empty/failed run exits
   non-zero — report that instead of claiming success.
2. Call the `chat_send_file` tool with that `local_path` (and a short `caption`).
   It delivers to THIS conversation's chat automatically — mp3/m4a arrive as
   Telegram's in-chat player. Do not ask the operator for a chat id.
3. Say what was generated (kind, duration, provider) in one line. For several
   variations use `--count N` and send each file.

Generation spends the operator's provider credits: generate what was asked, once.
Offer variations instead of silently re-rolling.

Notes:
- This is audio *generation*. Speech recognition / transcription and wake-word
  live under `navig voice` — use that for turning speech into text.
- The default output folder (`~/.navig/audio`) is fine from a chat — do not stop
  to ask where to save. Only ask when the operator wants a specific location
  (`--out <dir>`).
