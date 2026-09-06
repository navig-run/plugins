# navig-pipeline

The **content assembly line** — composes the NAVIG media plugin family into one flow:

```
acquire (navig-download) → transcribe (navig-audio) → script (navig-text)
     → narrate (navig-audio) → publish (navig-social)
```

```bash
navig pipeline status                                        # what's wired
navig pipeline run --topic "NAVIG v2 ships" --to x,telegram --dry-run
navig pipeline run --topic "…" --to x,telegram --narrate     # + audio voiceover
navig pipeline run --source clip.mp4 --to devto              # transcribe → post
```

## Why it exists

This is the payoff of "standalone-yet-wired plugins as OS system folders":
capabilities compose. `navig pipeline` is the one sanctioned orchestrator — it
reaches across plugins through the same public seams a user would (the
`navig.voice` transcribe shim, the generation facets, `navig_social` fan-out).

- **Every stage is optional.** A plugin that isn't installed simply drops its
  stage — reported, never a silent gap. `navig pipeline status` shows what's wired.
- **Safe by default.** `run` is a **dry-run**: it drafts the real caption and
  previews the fan-out, but performs no download / narration / publish. Pass
  `--live` to actually publish.
- **No hard deps.** The stage plugins are soft-imported at run time, so
  navig-pipeline installs and runs on its own.

## Install

```bash
pip install -e plugins/navig-pipeline
# or: navig store install pip:navig-pipeline
```
