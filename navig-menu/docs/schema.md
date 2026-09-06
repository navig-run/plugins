# `.navig/menu.json` — the override / AI-authoring contract

This is the **human- and AI-owned** menu definition. It is versioned (commit it), it always wins over
detection, and re-builds **merge** into it — your edits survive. It is shaped to be easy for an AI to
author (close to NAVIG's `SKILL.md` conventions).

Generate it with `navig-menu generate`. Use `navig-menu generate --json` to preview the exact file
without writing, then run without `--json` to create or merge `.navig/menu.json` and add
`"menu": "npx navig-menu"` to `package.json` when that script is missing. The generator
always adds `"menu:navig": "npx navig-menu"` as the stable generated-menu command. If
`menu` already exists, the generator keeps it and only adds `menu:navig`.

```jsonc
{
  "$schema": "https://menu.navig.run/schema/v1.json",
  "title": "SCHEMA",
  "purpose": "Civic network · maps · missions",
  "accent": "cyan",                       // cyan | blue | green | magenta | purple | yellow | red
  "packageManager": "pnpm",               // override detection if needed

  // Canonical action → command. Maps standard actions onto YOUR script names.
  "actions": {
    "dev": "pnpm dev",
    "build": "pnpm build",
    "test": "pnpm vitest run",
    "typecheck": "pnpm tsc --noEmit",
    "deploy": { "cmd": "pnpm wrangler deploy", "risk": "dangerous", "label": "Deploy to CF" }
  },

  "hide": ["prepare", "postinstall"],     // ids or canonical keys to drop
  "overrides": {                          // relabel / regroup / redescribe ANY detected script by id
    "dev:web": { "label": "Web · dev (:3000)", "description": "Next dev server", "group": "DEV SERVERS" },
    "deploy:api": { "group": "DEPLOY", "risk": "dangerous" }
  },
  "groups": [                             // custom section presentation + order (these come first)
    { "name": "QUICK START", "icon": "◆", "ascii": "*", "tone": "cyan" },
    { "name": "DEV SERVERS", "icon": "✦", "ascii": ">", "tone": "blue" }
  ],
  "extra": [                              // custom items beyond detected scripts (cwd optional)
    { "id": "studio", "label": "Drizzle Studio", "cmd": "pnpm drizzle-kit studio",
      "group": "Database", "cwd": "apps/api", "longRunning": true }
  ],
  "ui": {                                 // optional terminal preferences (edit live via the in-menu Settings panel)
    "recentLimit": 5,                     // 0 disables the Recent section
    "showDescriptions": true,
    "showCommandHints": true,
    "showCounts": true,                   // per-section item counts
    "showRiskBadges": false,              // inline confirm/danger tags in the hint column
    "showRecents": true,
    "showNavigSection": true,             // the UTILITIES rail (Settings/Customize/Regenerate/Refresh/Doctor)
    "glyphs": "emoji",                    // emoji | unicode | ascii (omit for auto-detect)
    "accent": "cyan",
    "density": "comfortable",             // comfortable | compact
    "layout": "flat",                     // flat (grouped sections) | list (one flat list) | categories (enter/back) | tree (nested filesystem)
    "bannerStyle": "cosmic",              // cosmic (multi-row box) | console (compact status line)
    "banner": {                           // cosmic banner rows (each also needs its data to exist)
      "spacedTitle": true,
      "endpoints": true, "stack": true, "git": true,
      "runtime": true, "date": true, "packages": true, "stats": true,
      "plugins": true                     // plugin-contributed banner lines (e.g. the store stat line)
    },
    "footer": "prod commands require review"
  },
  "plugins": {                            // plugin activation + options (toggle via the Plugins manager)
    "store": { "enabled": true, "settings": { "stat": true } }
  },
  "defaultHost": "local"                  // default execution target for relay mode
}
```

### Action shape

An action value is either a **string** (the command) or an **object**:

| Field | Meaning |
| --- | --- |
| `cmd` | The command (tokenized to argv; no shell). |
| `label` | Display label. |
| `description` | One-line help. |
| `risk` | `safe` \| `confirm` \| `dangerous`. |
| `longRunning` | Stream output + clean Ctrl+C. |
| `group` | `Development` \| `Quality` \| `Database` \| `Build` \| `Deploy` \| `Desktop & Store` \| `i18n` \| `Operations` \| `Utilities`. |
| `why` | Evidence note shown on the review screen. |

### UI shape

The menu UI is intentionally data-driven so an AI can create the same NAVIG-style menus across
projects without knowing terminal internals.

| Field | Meaning |
| --- | --- |
| `recentLimit` | Number of recent commands to show at the top. `0` disables recents. |
| `showDescriptions` | Show action descriptions when present. |
| `showCommandHints` | Show the executable command on each row. |
| `showCounts` | Show per-section item counts. |
| `showRiskBadges` | Show inline `confirm` / `danger` tags in the hint column. |
| `showRecents` | Show the Recent section. |
| `showNavigSection` | Show the UTILITIES rail (Settings / Customize / Regenerate / Refresh / Doctor / Edit / Quit). |
| `glyphs` | `emoji` \| `unicode` \| `ascii`. Omit to auto-detect from the terminal. |
| `accent` | Accent palette key (same set as top-level `accent`). |
| `density` | `comfortable` (section rules + breathing room) or `compact`. |
| `layout` | `flat` (grouped sections on one page), `list` (one flat list — no headers, no connectors), `categories` (a picker you enter with `enter` / leave with `esc`), or `tree` (a nested filesystem — namespaced scripts become folders-in-folders). |
| `bannerStyle` | `cosmic` (the multi-row box) or `console` (a compact box + one `⎇ branch · N changed · license · N commands` status line). |
| `banner` | Per-row banner toggles: `spacedTitle`, `tagline`, `endpoints`, `stack`, `git`, `runtime`, `date`, `packages`, `stats`, `plugins`. Each row also requires its data to exist. |
| `footer` | Extra footer text for local project rules. |

Everything under `ui` can be changed live from the in-menu **Settings** panel (`s`), which writes
back to `.navig/menu.json`. The **Menu Customizer** (`c`, or Settings → *Customize items*) lets you
hide/show individual commands — the hidden ids are stored in `hide`, and a category whose items are
all hidden disappears entirely. Both are reachable from the **UTILITIES** rail at the bottom of the
menu; press `g` anywhere to regenerate the whole contract from a fresh scan (your `ui` + `hide` are
preserved).

### Plugins

The `plugins` field records which plugins are active and their options. Toggle plugins from the
in-menu **Plugins** manager (key `p`, or Settings → *Plugins →*); the **About** screen (key `a`) lists
what's loaded. Plugins can change the banner, add sections/commands, claim scripts, and more — see
[`plugins.md`](plugins.md).

```jsonc
"plugins": { "store": { "enabled": true, "settings": { "stat": true } } }
```

Runtime recents live in `.navig/menu.recent.json` and are gitignored. Commit `.navig/menu.json`, not
the generated cache or recents file.

### Curating (overrides + groups)

To make a repo read like a hand-authored console (nice `App · action` labels, descriptions, custom
sections) without hiding + re-adding everything:

- **`overrides`** — a map of `scriptId → { label?, description?, group?, risk?, longRunning?, hide? }`.
  It patches the DETECTED script in place, so no duplicates. Great for relabeling `dev:web` →
  `Web · dev (:3000)` and routing it into a custom section.
- **`groups`** — an array of `{ name, title?, icon?, ascii?, tone? }` that defines each custom
  section's glyph + colour **and its order** (listed groups render first, before the canonical ones).

Pair these with `ui.bannerStyle: "console"` for the compact banner. See the NAVIG workspace's own
`.navig/menu.json` for a full worked example.

### The generated cache

`navig-menu` also writes `.navig/menu.cache.json` (gitignored) — pure detection output with
`evidence[]` + `confidence` per finding, keyed by a fingerprint of the project's interesting files.
You don't edit this; it's invalidated automatically when the project changes.
