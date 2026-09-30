# Changelog — navig-text

## Unreleased

### Added
- **`navig text lyrics` — lyrics that sit on a beat.** No AI, no network: it reads files.
  - `fit` — syllables per line against the time one bar gives (RU/EN/FR), the lines to split,
    fill or keep, the tempo a text naturally wants, `--all DIR --report FIT.md` for a library
    and `--beats DIR` for each text's nearest rendered beat. Moved here from navig-audio
    (`navig audio beat fit` stays, as an alias) because it is text work that needs no audio stack.
  - `scaffold` — an empty text cut to a beat's *measured* bar map (the sidecar's
    `measured.bars`): one section per block, one `_` slot per line with its syllable range.
    On a real 145 bpm render it reproduced, section for section, the form a hand-fitted text
    had been given.
  - `sheet` — a text's first hook + its voices table → a demo sheet for `navig audio beat demo`.
    A bar mark `/` reads as a comma, except next to punctuation or a dash; the beat path is kept
    as given, not resolved through a junction.
  - `words` (search a word bank; Notion's `# | phrase |` titles cleaned) and `rhyme` (spelling
    rhymes from the words you already use, strong before weak, never across scripts).

## 0.4.0 — 2026-09-28

### Added
- **Runs on its own, with your own AI.** `pip install navig-text` now gives a `navig-text`
  command with no navig installed — `navig-text gen|check` plus `navig-text design …`.
  Inside navig nothing changes: `navig text` and `navig design` use navig's AI as before.
  On its own it uses the first AI you have: `NAVIG_AI_BASE_URL` + `NAVIG_AI_MODEL` (any
  OpenAI-compatible server), a provider key (`OPENAI_API_KEY`, `OPENROUTER_API_KEY`,
  `ANTHROPIC_API_KEY`, `GROQ_API_KEY`, `GEMINI_API_KEY`, …), or a local Ollama — and says how
  to set one up when there is none. Documents save to `<navig config dir>/text` (honouring
  `NAVIG_CONFIG_DIR`) instead of a hard-coded `~/.navig/text`.

### Fixed
- **Docs named `navig generate --modality text`, which exits 2.** `--modality` is an option of
  the `gen` subcommand, not of the `generate` group. Corrected in the module docstrings and the
  README to `navig generate gen --modality text`. (The historical CHANGELOG entries are left as
  written — they record what was true when shipped.)

## 0.3.0 — 2026-07-19

### Added
- **Per-space design systems: `navig design tokens save --space <name>` / `show --space <name>`.**
  Tokens persist into that space's `.navig/design` (`tokens.json` + `tokens.css`) instead of the
  global `data_dir()/design`, resolved via `spaces.resolver.discover_space_paths` — so each project
  keeps its own design system. No `--space` = global (unchanged). An unknown space is a clean error,
  never a silent global write.

## 0.2.0 — 2026-07-18

### Added
- **`navig design` — the agent-backed design surface behind the Design Mode browser lens**
  (in the NAVIG Dock extension). Lives here (not navig-generate) because it is an agent-backed
  *text/markup* transform sharing the same core AI client — code/markup is text.
  - `navig design edit` restyles/rewrites ONE HTML element from a natural-language instruction
    (element HTML + prompt → revised HTML as JSON on stdout) via core's AI client — no new deps,
    no gateway route. The lens reaches it through the existing `host.run()` + a `--b64` payload.
  - `navig design check` reports AI-provider readiness (parity with `navig text check`).
  - `navig design tokens save` / `show` persist an extracted design system (colors / type scale /
    weights / spacing / radii) as `tokens.json` + `tokens.css` under `data_dir()/design`
    (honors `NAVIG_DATA_DIR`). Refuses an empty/malformed set; CSS values are sanitized on write.

## 0.1.0 — 2026-07-09

### Added
- **navig-text plugin — the TEXT facet (Phase 3.2).** `navig text gen` drafts Markdown
  (drafts / articles / captions / fan-out briefs) via core's AI client — no external deps,
  no new keys (uses whatever provider you've connected). `--count` produces distinct variants;
  `--system` overrides the system prompt; `navig text check` reports provider readiness.
- **Registers a `TEXT` backend** into core's generation registry
  (`navig.media.types.register_generator`), so `navig generate --modality text` and the deck
  media route dispatch through this plugin. Generated docs land as `MediaModality.TEXT` rows in
  the shared refs library (new `text/` category + `.md` ext in core), so drafted text flows into
  the fan-out and the content assembly line. The second facet proving the register_generator pattern.
