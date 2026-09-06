---
name: podcast-episode
description: Compile a podcast episode from a Markdown scenario into finished audio — multiple tracks, multiple languages, in a cloned voice, with subtitles and transcripts. Use when the user wants to turn a written script, outline or episode plan into audio, produce an episode in more than one language, clone their voice, or estimate what generating an episode will cost.
activation_keywords: [podcast, episode, scenario, script to audio, voice clone, narration, audiobook, dub, multilingual audio, subtitles, transcript, show notes]
metadata:
  version: 1.0.0
  toolsAllowed:
    - bash_exec
---

# Podcast episode compilation

Turns a Markdown scenario into an **album of audio tracks** — one file per segment, not
one long recording — in as many languages as needed, using the user's own cloned voice.

## The pipeline

```
rough notes ──draft──▶ scenario.md ──translate──▶ scenario.<lang>.md
                          │  (the user reviews and edits this)
                          └────render────▶ tracks + master + .srt + transcripts
```

## Commands

```bash
navig audio check                                  # plan, credits left, can-I-clone
navig audio voices [--mine]                        # voice ids for the frontmatter
navig audio clone "Serio" ./samples                # instant voice clone → voice_id

navig audio draft Script.md --lang fr --episode 0  # rough notes → strict scenario
navig audio plan ep000.fr.md --lang fr,en          # cost estimate; spends nothing
navig audio translate ep000.fr.md --to en          # → ep000.en.md
navig audio render ep000.fr.md --lang fr,en        # build the album
navig audio render ep000.fr.md --track 03          # redo one track only
navig audio publish ./episodes/ep000-slug          # show what would upload (not wired)
```

## Rules

1. **Always run `navig audio plan` before `render`.** Generation is billed per
   character; a 45-minute episode is roughly 38,000 credits *per language*. `plan`
   spends nothing and checks the total against the remaining balance.
2. **Never skip the review step.** `draft` and `translate` write files for the user to
   read. Do not chain straight into `render` without saying the file is there to check —
   a misjudged line only becomes audible after it has been paid for.
3. **Cloning needs a paid ElevenLabs plan** (Starter or above); the free tier has no
   cloning *and no commercial licence*. Run `navig audio check` to see which applies
   before suggesting a clone.
4. **Re-running is free** where nothing changed — clips are cached by content. Never
   warn the user off re-rendering.

## Scenario format

```markdown
---
episode: 0
slug: ouverture-du-terminal
title: "NobiCast EP0 — Ouverture du Terminal"
lang: fr
model: eleven_multilingual_v2
default_speaker: NOBI
voices:
  NOBI: {voice_id: "<id>", stability: 0.5, similarity_boost: 0.8}
  SERIO: "<id>"
---

## 01 — Intro & Sonic Branding

[music: cyberpunk modem swell, warm, 8s]
[sfx: 56k handshake]

NOBI: Connexion établie. Terminal en ligne. Bienvenue dans NobiCast.

> a production note — never spoken, never billed
```

- `##` heading → one track (one mp3)
- `SPEAKER:` → voice change; a bare paragraph uses `default_speaker`
- `[music: …]` / `[sfx: …]` → generated cues, kept in reading order
- blockquotes, bullets and HTML comments → notes, excluded from audio **and** from cost

## Output

```
episodes/ep000-<slug>/
  fr/  01-*.mp3 … 08-*.mp3  master.mp3  master.srt  *.srt  *.txt  chapters.json
  en/  (the same)
  metadata.json
```
