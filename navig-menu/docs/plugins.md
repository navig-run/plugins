# Plugins

`navig-menu` is extensible. A plugin can **change the banner** (add lines/phrases), **overwrite text**
(title / tagline / prompt), **add sections and commands**, **claim/relabel** your existing scripts,
**add settings**, expose **actions with handlers**, and contribute **About** lines. Plugins activate
and deactivate from the in-menu **Plugins** manager (key `p`), and their state is saved to
`.navig/menu.json`.

There are two tiers:

| Tier | What it is | Safety |
| --- | --- | --- |
| **declarative** | a JSON / YAML file — pure data | 100% safe, no code runs |
| **programmatic** | a JS module exporting `definePlugin({...})` | runs code (only in the menu phase); adds dynamic `handlers` |

> Plugins run **only in the model/menu phase** — never during the pure detection scan. The cached
> manifest (`.navig/menu.cache.json`) stays execution-free and secret-free. See `AGENTS.md`.

## Where plugins come from

Discovered low→high precedence (local wins on id collision):

1. **Built-in** — bundled with the tool (e.g. `example`, `store`).
2. **npm** — a dependency named `navig-menu-plugin-*` (or scoped `@you/navig-menu-plugin-*`).
3. **Local** — a file in `.navig/plugins/`: `*.json`, `*.yaml`/`*.yml` (declarative) or `*.mjs`/`*.js`
   (programmatic).

A plugin activates when `detect()` matches your project, unless you flip it in the Plugins manager.
Activation + per-plugin options are stored in `.navig/menu.json`:

```jsonc
{
  "plugins": {
    "store":   { "enabled": true, "settings": { "stat": true } },
    "example": { "enabled": false }
  }
}
```

## Build your first plugin in 10 lines (declarative)

Create `.navig/plugins/hello.json`:

```json
{
  "id": "hello",
  "bannerPhrases": ["✦ powered by a plugin"],
  "text": { "tagline": "our team console" },
  "sections": [
    { "group": "Team", "meta": { "emoji": "👥", "ascii": "t", "tone": "cyan" },
      "actions": [{ "id": "team.docs", "label": "Open docs", "cmd": "open https://our.wiki" }] }
  ],
  "about": ["hello plugin by our team"]
}
```

Open the menu, press `p`, toggle **hello** on. Done — no build step.

### Auto-activate it

Add a `detect` block (any match activates it):

```json
{ "id": "hello", "detect": { "scripts": "^team:", "files": ["team.config.json"], "deps": ["@acme/sdk"] } }
```

## Programmatic plugins

For dynamic work (fetching stats, computing lines), write `.navig/plugins/store.mjs`:

```js
export default {
  id: "downloads",
  tier: "programmatic",
  detect: (ctx) => ctx.hasFile("store.config.json"),
  contribute: (ctx) => {
    const stats = ctx.readCache("stats");           // .navig/plugins-cache/downloads/stats.json
    return {
      bannerLines: stats ? [{ text: `Downloads: ${stats.total}`, tone: "green" }] : [],
      settings: [{ key: "banner", label: "Downloads · banner", type: "toggle", default: true }],
      sections: [{ group: "Store", meta: { emoji: "🏪", ascii: "$", tone: "magenta" },
        actions: [{ id: "store.refresh", label: "Refresh stats", internal: "refresh" }] }],
      about: ["downloads plugin"],
    };
  },
  handlers: {
    refresh: async (a) => {
      await a.run("node", ["scripts/fetch-stats.mjs"]);   // argv array, no shell
      a.writeCache("stats", { total: 1234 });
      a.notify("stats refreshed");
    },
  },
};
```

Use `definePlugin({...})` from `navig-menu/plugin` if you want type-checking.

## API reference

### `MenuPlugin`

| Field | Type | Notes |
| --- | --- | --- |
| `id` | string | unique id (also the `.navig/menu.json` key) |
| `tier` | `"declarative"` \| `"programmatic"` | |
| `version` | string? | shown in About / Plugins |
| `detect(ctx)` | `(PluginContext) => boolean` | pure; auto-activation predicate |
| `contribute(ctx)` | `(PluginContext) => PluginContribution` | pure; what the plugin adds |
| `handlers` | `Record<string, (PluginActionContext) => void\|Promise<void>>` | for `internal` actions |

### `PluginContribution`

| Field | Effect |
| --- | --- |
| `text` | overwrite `title` / `tagline` / `purpose` / `prompt` |
| `bannerPhrases` | dim lines under the banner title |
| `bannerLines` | `{ text, tone? }[]` — a divided banner group (e.g. a stat line) |
| `sections` | `{ group, meta?, actions[] }[]` — new command sections |
| `claims` | `{ match, group, labelPrefix?, risk? }[]` — reroute detected scripts (regex over the script id) |
| `settings` | `{ key, label, type:"toggle"\|"cycle", values?, default }[]` |
| `about` | extra About lines |

Action specs (`sections[].actions[]`): `cmd` (a command), `delegateTo` (a detected script id), or
`internal` (a `handlers` key) — plus `label`, `description`, `risk`, `longRunning`.

### `PluginContext` (read-only — `detect` / `contribute`)

`root`, `manifest`, `scripts`, `settings` (this plugin's resolved options), `cacheDir`,
`hasFile(rel)`, `hasDep(...names)`, `readCache(name)`.

### `PluginActionContext` (handlers only — sanctioned side effects)

Everything above plus:
- `run(launcher, argv, cwd?)` — spawn a command (argv array, no shell); returns the exit code.
- `capture(launcher, argv, cwd?)` — run + capture `{ stdout, exitCode }` (no shell).
- `select({ message, choices })` / `input(message)` / `confirm(message)` — interactive prompts.
- `writeCache(name, data)` / `readCache(name)` · `notify(message)` · `theme`.

## Ideas & roadmap

Want to build one but need inspiration? [`plugin-ideas.md`](plugin-ideas.md) has a worked list —
`npm-publish`, `chrome-extension`, a generic `api-data-banner`, `github`, `docker`, `coverage`,
`env-doctor`, and more — each with its detect rule, banner line, and actions.

## Bundled plugins

- **`store`** — a full Microsoft Store publishing pipeline (native Partner Center engine); see below.
- **`wrangler`** — a Cloudflare / Wrangler command palette (Workers & Pages: deploy · dev · tail · D1
  · KV · R2 · secrets · Queues / Vectorize / Hyperdrive · types · auth). Auto-activates in any
  Cloudflare project, parses its `wrangler.toml`/`.jsonc`/`.json` **offline** to show exactly the
  bindings it declares, and drives the real `wrangler` CLI with production-safe confirmations. Full
  reference: [`wrangler-plugin.md`](wrangler-plugin.md).
- **`github`** — a GitHub command palette (PRs · issues · Actions/CI · releases · repo/auth) driven by
  the `gh` CLI. Auto-activates in any repo with a GitHub remote, shows an `owner/repo` banner (with
  cached stars / open-PR / CI once refreshed), and confirms the side-effecting actions (PR checkout,
  release create). Full reference: [`github-plugin.md`](github-plugin.md).
- **`env-doctor`** — keeps your local env in sync with the project's example (`.env.example`,
  `.dev.vars.example`, …). Banner shows how many documented keys are missing; **Check** lists them and
  **Scaffold** appends empty `KEY=` lines. Works purely on key **names** — never reads values. Full
  reference: [`env-doctor-plugin.md`](env-doctor-plugin.md).
- **`ports`** — kill / inspect / find local ports (auto-activates when the project serves on a port).
  A great, compact example of an interactive plugin: it uses `capture` to read `netstat`/`lsof`,
  `select`/`input`/`confirm` to drive the flow, `run` to `taskkill`/`kill`, `node:net` to find a free
  port, and options (`range`, `confirm`, `force`). If a kill fails it prints the holding process.
- **`devhost`** — local `.test` domains with trusted HTTPS (auto-activates for web projects). Drives
  the `navig devhost` CLI (hosts + mkcert + a stdlib TLS relay) via `run`/`capture` to set up / start
  / open / status / remove a `https://<name>.test` in front of a dev server; guards for when the
  `navig` CLI isn't installed.
- **`example`** — the minimal template (off by default).

## Distributing an npm plugin

Publish a package named `@you/navig-menu-plugin-foo` (or `navig-menu-plugin-foo`) whose default
export is a `MenuPlugin`. Users `npm i -D` it and it's auto-discovered; they toggle it in the Plugins
manager. Ship the declarative form for zero-risk plugins.

## Security / trust

- Declarative plugins are pure data — always safe.
- Programmatic plugins run only in the menu phase, never during detection. Built-ins are first-party;
  npm plugins run only if you installed them (same trust as any devDependency); local plugins live in
  your own repo. No remote download/eval.
- `run` is the only spawn primitive (argv array, no shell). Plugin/delegated actions carry a `risk`
  tier and flow through the same typed `dangerous` confirmation — `--yes` can't bypass it.
- A broken plugin is isolated: it's skipped with a warning (shown in Doctor / About), never a crash.

## The built-in `store` plugin

A full **Microsoft Store publishing pipeline** with a native TypeScript Partner Center engine —
no external CLIs, no PowerShell. It is entirely project-agnostic: any MSIX project (one app or a
whole catalog) describes its apps in a `store.config.json` at the repo root and gets:

- **Publish / submit** — dry-run (prepare + upload, no commit) → package-only → manual go-live →
  Immediate go-live (typed confirmation). Reuses non-active pending drafts so manual Partner
  Center work (IARC, data declaration) survives re-runs.
- **Submission status**, **Prices audit** (local config vs live tiers), **Ensure Pro add-on**
  (durable IAP lifecycle + price tiers), **Pull live listing** (Store → local metadata.json),
  **Refresh stats** (downloads/revenue → cache), **Check credentials** (names only, never values).
- A banner stat line aggregated across apps, plus an "in certification / failed" line from the
  status cache.

Auto-activates on `store:*`/`steam:*` scripts or a `store.config.json` / `.store-creds.json`.
Without a `store.config.json` it degrades to the original skeleton behavior (claims your
`store:*`/`steam:*` scripts into a **STORE** rail, delegates stats to project scripts).

Credentials: `AZURE_TENANT_ID` / `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET` (env, or a gitignored
creds file named by `credsFile`). The Azure AD app must be added in Partner Center → User
management → Azure AD applications with the Manager role. Values are read only at call time and
never cached, logged, or shown.

Full reference — config contract, per-app fields, add-on rules, first-publish checklist, and the
integration smoke runbook: [`store-plugin.md`](store-plugin.md).

The Steam side (build → stage → `app_build_*.vdf` → `steamcmd`) is still reserved for a future
`navig-menu-plugin-steam`; the plugin keeps claiming `steam:*` scripts in the meantime.
