# navig-text

AI **text generation** for NAVIG — drafts, articles, captions, and fan-out briefs.
A first-party, free, toggleable plugin, and the **TEXT facet** of NAVIG's shared
media-generation engine.

```bash
navig text gen "explain vector databases in 5 bullets"          # a quick draft
navig text gen "NAVIG weekly update" --kind article --out ./out # a full article
navig text gen "ship faster with NAVIG" --kind caption          # one caption
navig text gen "NAVIG v2 launch" --kind brief                   # a fan-out brief
navig text check                                                # is a provider ready?
```

## `navig design` — the Design Mode surface

The agent-backed surface behind the **Design Mode** browser lens (in the NAVIG
Dock extension). Same core AI client as text generation — code/markup is text, so
it lives here rather than in a separate plugin.

```bash
navig design edit --prompt "make this a bold dark CTA" --html '<button>Buy</button>'
navig design check                                    # is an AI provider ready?
navig design tokens show                              # the global design system
navig design tokens show --space acme                 # a space's design system
```

- `design edit` returns the revised element HTML as JSON on stdout; the lens
  base64-encodes the element + your instruction, calls it via `host.run()`, then
  applies the result live (sanitized) with undo.
- `design tokens save`/`show` persist a page's extracted palette / type scale /
  spacing as `tokens.json` + `tokens.css` under `~/.navig/data/design` (global),
  or into a space's `.navig/design` with `--space <name>` — one design system
  per project.

## How it fits

- **No external deps.** Generation runs through core's AI client
  (`navig.agent.ai_client`) — the architectural law that all inference lives in
  `navig/agent/`. Whatever provider you've connected (`navig connect`) powers it.
- **Registers a facet.** At boot it plugs a `TEXT` backend into the core
  generation registry (`navig.media.types.register_generator`), so
  `navig generate gen --modality text` and the deck media route dispatch through it.
- **Refs-library native.** Generated Markdown lands as `MediaModality.TEXT` rows
  in the same versioned refs library as images / video / audio — so a drafted
  caption or brief flows straight into `navig social fan-out` and the content
  assembly line.

## Install

```bash
pip install -e plugins/navig-text     # dev (editable)
# or, for the user's real CLI:
navig store install pip:navig-text
```

Part of the NAVIG media plugin family: **navig-image · navig-video · navig-audio ·
navig-text** — each a thin facet over one shared engine.
