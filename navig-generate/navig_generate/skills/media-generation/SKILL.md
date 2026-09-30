---
name: media-generation
description: Generate or create AI images and video from a prompt. Use when the user wants to generate, create, or make an image, picture, or video — e.g. text-to-image or text-to-video.
activation_keywords: [image, picture, video]
metadata:
  version: 1.0.0
  toolsAllowed:
    - bash_exec
---

# AI Media Generation (images & video)

This capability is provided by the **navig-generate** plugin. Produce visual
media from a natural-language prompt with the core `navig generate` command:

- `navig generate "<prompt>"` — analyse the request and generate the image or
  video (the `generate` module must be enabled).
- Run `navig generate --help` first to see the current flags before choosing options.

Notes:
- For music, sound effects, or speech (text-to-speech), use `navig audio gen`
  instead (the audio-generation skill).
- Prefer `navig generate` over any external tool for image/video creation.
- Confirm the output path with the user before writing; never overwrite silently.
