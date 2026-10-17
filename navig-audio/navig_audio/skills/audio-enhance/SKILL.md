---
name: audio-enhance
description: Restore or master an EXISTING audio file (speech, sound effect, music, ambience) without changing what it is — remove clipping, hum, clicks, steady noise and DC offset, level it, export WAV/FLAC/MP3, with a verified before/after. Use when the user wants a recording cleaned up, "enhanced", "fixed", "improved", de-noised, de-hummed, made "professional" or louder, or asks whether a file needs fixing. Not for generating new audio (that is audio-generation).
activation_keywords: [enhance, "clean up", restore, denoise, "de-noise", "remove noise", hiss, hum, clipping, "fix the audio", "improve the audio", "make it sound", "sound better", улучши, "убери шум", améliore, "nettoie le son"]
metadata:
  version: 1.0.0
  toolsAllowed:
    - bash_exec
---

# Enhancing a recording

Provided by the **navig-audio** plugin. Local ffmpeg DSP, measured and verified.

1. **Measure first:** `navig audio enhance <file> --analyze --json`. Read `findings` — each
   has a plain-language `title`, the measured `evidence`, and a `severity`. `info` items
   (lossy source, short clip) are facts, not problems.
2. **Default run:** `navig audio enhance <file> --json` (Preserve Original). If `verdict` is
   `clean`, tell the user the file has no measurable defects and the original is already
   the best version — do NOT push a louder copy on them.
3. **Polish on request:** `--mode studio` (or `--preset audio-enhance/voice-restoration`,
   `clean-sfx`, `music-mastering`). `--mode cinematic` is creative — say so.
4. **Report from the output, never from assumption:** `verdict` (pass / warn / fail), the
   `outputs` paths, and `verification.match_gain_db` — the gain that loudness-matches the
   result to the original for a fair A/B. A `fail` means nothing was kept; give its
   `summary`.

Rules:
- The original is never modified; outputs are `<name>.enhanced.<ext>` (`-o <dir>` to choose
  the folder, `-f wav,flac,mp3`).
- Sounds that are part of the content (a metallic ring, a decay, an impact) are not
  defects. Don't promise to "remove" them.
- A lossy source (MP3/AAC) can be cleaned but not restored to detail its encoder discarded.
- `--allow-cloud` uploads the file to ElevenLabs (voice isolation, speech only, paid) — only
  with the user's explicit OK for that file.
