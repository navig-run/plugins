# Changelog — navig-text

## Unreleased

### Fixed (0.5.1)
- **The TEXT generation facet registers without navig.** It imported the registry through
  navig's alias (`navig.media.types`), so a standalone install silently skipped it; it now
  uses navig-media-core first (`navig-text[media]` adds it standalone).

### Added
- **`navig text gen --preset <id>` (repeatable) and `--var key=value`.** Applies saved presets
  (`navig presets list --tool text`) — instructions, approved context and the output format —
  without pasting them again. The engine backend accepts `preset_system`, appended after the
  kind's base prompt; the request itself is never folded into the system prompt.
- **`navig text channel` — a channel's master texts.** Sync, lint, sweep and live data for
  the workspace that renders a channel (media stays there; only the code lives here).
  - `sync` — push the master (`out/ALL-TEXTS*.md`) into the channel's scripts, positional by
    structure: a literal swap must find EXACTLY ONE occurrence or it is reported, never
    written. `--apply` writes, `--check` exits 1 when files would change (CI), `--only`
    narrows to one target, `--json` for machines. **Fixes the lore double-list bug the old
    `.tmp/sync_rev6.py` had** — it sliced the first `{` inside `LORE = [` instead of the
    list bracket, nesting the list as its own first element (compiles, then crashes the next
    run); `sync` now matches the list bracket and reports a corrupted `LORE` instead of
    compounding it. A regression test pins both.
  - `lint` — the rules the master's own header declares: unique lore ids (checked on the raw
    text — the parser folds duplicates), beat counts agreeing with the scripts, unpaired
    red-word asterisks, vendor names in citizen text, role-word casing (`--strict` promotes
    notes), RU world terms that lost their capital.
  - `sweep` — a literal terminology sweep (cities → Zones by default, rules in
    `channel.json`), masking tokens/tags/code spans so `{citiesOpen}`, `#citylife` and
    `` `code` `` survive. Dry by default; `--apply` writes; `--check` for CI.
  - `data` — the live numbers the cards quote: public read → disk cache → loud failure.
    A fetch failure falls back to the cache and says `_source: cache` with its age;
    `--strict` refuses instead; no cache + failure is an error. Shape from the workspace
    (`data.shape` in `channel.json`) or the built-in summary/board shape.
  - Workspace resolution: `--workspace/-C` → `NAVIG_WORKSPACE` → the default channel dir.

### Added — lyrics
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

### Fixed
- **`channel lint` reads the master's story cards and fails on drift.** `## 8 · ARCHIVE
  CARDS` is now parsed (a `cards` key on the parsed master) and compared field by field
  against `story_kit.STORIES` — that pair is not in `sync`'s patch set, so a hand-fix could
  land in one file while both gates stayed green (it happened: ST-002's RU sub said «город»
  in the master and «Зону» in the script). A card present in one place and not the other, or
  any `head`/`sub`/`ru_head`/`ru_sub` disagreement, is an error. Also `attach_workspace`
  now prunes the directories it attached on an earlier call: a module missing from THIS
  workspace fails to import instead of silently resolving from a previous fixture's
  directory still sitting on `sys.path`.

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
