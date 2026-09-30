# Changelog

All notable changes to `navig-menu`.

## 1.7.0

### New built-in `env-doctor` plugin — keep your env in sync with the example (names only)
- Auto-activates when the project ships an example-env file — `.env.example`, `.env.*.example`,
  `.sample`/`.template`, or **`.dev.vars.example`** (the Cloudflare Workers convention). Pairs each
  example with the real file it documents (`.env.example`→`.env`, `.dev.vars.example`→`.dev.vars`, …).
- **Banner** `env · N keys missing` / `env · all keys set`. **Check** lists present vs missing keys
  (and any extras); **Scaffold missing keys** appends empty `KEY=` lines for the gaps (confirmed).
- **Names only, never values.** Env files are read solely to compare key *names* — no value is ever
  captured, printed, logged, or written (the scaffold writes empty `KEY=`). An explicit test proves a
  value never leaks even when it contains `=` or a URL.
- +11 unit tests; docs: [`docs/env-doctor-plugin.md`](docs/env-doctor-plugin.md).

## 1.6.0

### New built-in `github` plugin — a GitHub command palette (via the `gh` CLI)
- Auto-activates in any repo with a **GitHub remote** (github.com or Enterprise), read offline from
  `.git/config` (all of git's URL forms; origin preferred). Surfaces **Status · Pull requests ·
  Issues · Actions/CI · Releases · Repo overview · Open on GitHub · Refresh stats · Auth** — list
  views drill down with a picker (view · open in browser · check out a PR · re-run failed CI · …).
- **Banner.** Shows `owner/repo` immediately; **Refresh stats** adds cached
  `★<stars> · <open PRs> PRs · CI <✓/✗/…>` (no network call in the banner itself).
- **Safe by construction.** Runs `gh` as an argv array (no shell); `gh` owns its own auth so no
  credentials pass through the plugin. Side-effecting actions confirm first — PR **check out**
  (switches your branch) and **release create** (publishes) and CI **re-run**. If `gh` isn't
  installed, actions report a clear install hint instead of failing obscurely.
- Distinct from the standalone `navig-github` Python plugin (backup/mirror/export) — different surface.
- +18 unit tests (remote URL parsing across all forms, `.git/config` parsing, argv builders, banner,
  end-to-end detect/contribute); docs: [`docs/github-plugin.md`](docs/github-plugin.md).

## 1.5.0

### New built-in `wrangler` plugin — a Cloudflare / Wrangler command palette
- Auto-activates in any Cloudflare project (a `wrangler.toml`/`.jsonc`/`.json`, a
  `wrangler`/`@cloudflare/*` dependency, or a `wrangler`-invoking script) and drives the real
  `wrangler` CLI: **Deploy · Dev · Tail · Deployments/rollback · D1 · KV · R2 · Secrets · Queues /
  Vectorize / Hyperdrive · Generate types · Whoami · Login**.
- **Config-aware.** Parses the project's own wrangler config **offline** (TOML via `smol-toml`,
  JSONC via a string-aware stripper) and shows exactly the bindings it declares — a D1-only worker
  gets a D1 rail but no KV/R2; a KV+R2+Vectorize worker gets those and no D1. Classifies Worker vs
  Pages (incl. `[assets]` and pages-by-script). A banner stat line summarizes it
  (`Cloudflare · <name> · <type> · D1×1 · KV×1 · … · env: production/staging`), with an optional
  cached `whoami` account line.
- **Safe by construction.** Every action runs wrangler as an argv array (no shell). Production/
  `--remote` ops (deploy, D1 `--remote`, KV/R2 writes & deletes, rollback, secret delete) confirm
  explicitly; `local` vs `remote` is always explicit. Secret **values** never leave wrangler — only
  the name passes through the plugin. Settings: `banner`, `remoteConfirm`, `runner`
  (`auto`/`npx`/`pnpm`/`bun`), `whoami`. The repo's own wrangler scripts stay in their normal DEPLOY/
  scripts rails (the `cf.*` actions already cover them — no duplicate "Deploy" in the rail).
- New dependency: `smol-toml` (tiny, zero-dep TOML parser). +28 unit tests; docs:
  [`docs/wrangler-plugin.md`](docs/wrangler-plugin.md).

### New built-in `devhost` plugin — local `.test` HTTPS domains from the menu
- Auto-activates for web projects (any `package.json`, or a detected dev-server port) and drives the
  `navig devhost` CLI: **Set up HTTPS domain · Start HTTPS (relay) · Open · Status · Remove**. Gives a
  dev server a trusted `https://<name>.test` (hosts entry + mkcert cert + a stdlib TLS relay) right
  from `navig menu`.
- **Migrated in** from a copy-installed shim: `navig devhost menu install` (which dropped a `.mjs`
  into `.navig/plugins/`) is retired; the integration now ships **with navig-menu** as a first-class
  built-in, so it appears automatically and updates with the menu.
- Degrades gracefully when the `navig` CLI isn't installed (reports it, never throws). +8 unit &
  menu-model integration tests.

## 1.4.0

### Store plugin — onboarding, local certification & `.env` creds (store plugin `2.1.0`)
- **`Initialize store config`** — a config-less project now shows one action that scaffolds
  `store.config.json` + a `metadata.json` stub, auto-detecting Tauri / .NET-MSIX build output for the
  package glob and identity. The only thing it can't detect — the Partner Center Store product ID — is
  emitted as a clearly-marked `SET-STORE-PRODUCT-ID` placeholder. Detection now also fires for any
  Tauri/MSIX app (not just ones that already have store config), so onboarding a brand-new app is a
  single action instead of a chicken-and-egg.
- **`Certify (WACK)`** — runs the Windows App Certification Kit (`appcert.exe`) on the built
  `.msix`/`.msixbundle` beside the configured `package` and reports the verdict + failing tests. The
  same cert pass the Store runs server-side, caught locally before submission.
- **Build orchestration** — new optional `apps[].build` command (bump + compile + pack). `Publish`
  gains a **Rebuild & submit** mode that runs it then submits, and auto-builds when no package matches
  — the legacy "ship a new version" flow as one action. Version-bump policy stays in that command.
- **`Open Partner Center`** — opens the app's submissions page in the browser (no auth).
- **`.env` credentials** — `AZURE_*` are now resolved env → gitignored root **`.env`** → `credsFile`
  JSON (only those three keys are read from `.env`; real env still wins). *Check credentials* reports
  `.env` presence too. Secret-safety unchanged (names only, never values).
- Docs: `docs/store-plugin.md` documents the new actions, `.env` resolution, and the explicit
  build/submit boundary (the plugin submits a prebuilt package; MSIX build stays a per-app script the
  STORE rail auto-claims). +10 unit tests (init scaffolding, WACK parsing, `.env` creds).

## 1.3.0

### Catalog import, AI organize & a fixing doctor
- **`import <catalog> [--write]`** — deterministically fold a hand-curated command catalog (the
  `navig-catalog` v1 shape: `categories[]→items[]` of `{exec,cwd,label,desc,danger}`) into
  `.navig/menu.json` as `extra[]`/`groups[]`, dedup-demoting the detected root scripts it supersedes
  (by exact command and by package+canonical intent) and pruning inert overrides. Merge-preserving
  and idempotent; interactive/`special` and shell-operator items are skipped with an audit, not
  silently dropped.
- **`organize [--write]`** — report gaps (missing canonical actions, orphaned overrides) and use AI
  (opt-in) to write a description for every still-unexplained command; batched so large monorepos fit
  the token budget. Also an **"Organize with AI"** rail action in the live menu.
- **`doctor --fix`** — tiered, confirm-gated repairs (missing `menu` script, orphaned overrides,
  and, with `--ai`, a suggested body for a missing canonical) that write a timestamped
  `package.json` backup to `.navig/backups/` first. Broken `cd` paths are advisory only.
- **`layout: "projects"`** — an optional menu view grouped by owning workspace package, plus a
  command-description fallback (an un-curated script shows the command it actually runs).

### Package-manager-aware menu script, safer & prettier
- The generated `menu` script now uses the **detected package manager's** exec runner
  (`pnpm dlx` / `bunx` / `yarn dlx` / `npx --yes navig-menu`); the recognition set learns all forms
  so a hand-written `menu` / `pnpm menu` is **never** overwritten (only a `menu:navig` alias is added).
- Unified, consistent non-interactive report styling across `doctor` / `import` / `organize` / `scan`
  (shared `ui/report.ts`; glyph + word, ASCII-degrading under `--plain`).

### Programmatic model API
- New **`navig-menu/model`** export exposing the resolver (`buildMenuModel` / `scanProject` /
  `loadDefinition` + types) so other tools can consume the resolved menu in-process; `list --json`
  is documented as the equivalent palette payload.

### Microsoft Store publishing (built-in `store` plugin, v2)
- The `store` plugin skeleton is now a **full Microsoft Store pipeline** with a native TypeScript
  Partner Center engine (no PowerShell, no external CLIs; compiles into the single binary):
  publish/submit with dry-run → package-only → manual → typed-confirm go-live, submission status,
  durable add-on (IAP) lifecycle + price-tier mapping (advanced & legacy schemes), prices audit,
  analytics refresh, and live-listing pull into local `metadata.json`.
- Driven by a generic **`store.config.json`** at the project root — any MSIX app, one or many per
  repo; the plugin carries zero product-specific defaults (category/cert-notes/add-on naming all
  come from config). Single-app projects skip the pickers. Without the config it degrades to the
  previous skeleton behavior (script claims + stats delegation).
- Hardened semantics ported from a production pipeline: pending-draft reuse (preserves manual
  IARC/data-declaration work), stale-SAS detection, committed-vs-uncommitted package/screenshot
  swap rules, first-publish guards (forced Free pricing, listing required, Desktop-only device
  families) with a privacy/IARC checklist on CommitFailed, add-on pricing read from the published
  app submission, commit retry with post-error status re-check, and last-known-good analytics
  caching. Secrets are read only at call time and never cached, logged, or displayed.
- New dependency: `fflate` (pure-JS zip for the submission upload).
- Fixed: contribute-phase `readCache`/`cacheDir` are now plugin-scoped
  (`.navig/plugins-cache/<plugin>/`), matching handlers — a banner can finally read what its own
  handler cached.
- Docs: [`docs/store-plugin.md`](docs/store-plugin.md) (config contract + smoke runbook).

### Headless plugin actions
- `navig-menu run <action>` now executes **plugin internal actions** (e.g. `run store.status`) —
  it loads plugins fully (including programmatic local `.mjs`), dispatches the handler in-process,
  and returns honest exit codes for CI. Risk gating applies: `confirm`/`dangerous` actions still
  prompt (typed confirmation for dangerous — `--yes` can't bypass it).

### Fixed
- **Windows crash on exit after network actions** — `Assertion failed: !(handle->flags &
  UV_HANDLE_CLOSING), src\win\async.c` when the CLI force-called `process.exit()` right after
  `fetch()` (reproducible with plain `fetch().then(() => process.exit(0))` on Node 24). The CLI
  now sets the exit code, releases the TTY, and lets the event loop drain naturally, with an
  unref'd 2 s watchdog as the only forced-exit fallback.
- Transient-retry warnings no longer print full query strings (analytics URLs were noise);
  `Refresh stats` prints per-app progress so long rate-limited runs are visibly alive.

### Hardening (review pass)
- `doctor --fix` no longer lets AI author a body for a destructive canonical (`reset`, `migrate`,
  `seed`, `deploy`, `clean`) — those must be written by hand, even with `--yes`.
- `doctor --fix` tolerates a malformed `package.json` (clear message instead of a crash) and backs up
  `.navig/menu.json` before pruning orphaned overrides.
- `import` deduplicates a repeated catalog id (first wins) instead of emitting a doubled entry.
- Store: an app commit now re-checks submission status after a gateway error (matching the add-on),
  so a landed-but-5xx commit isn't reported as failed; the add-on status poll tolerates transient
  errors; an ambiguous empty status keeps polling instead of falsely reporting "accepted".
- Store: a paid add-on with no exact price tier is refused (was: silently mapped to the nearest
  anchor, capping at $9.99) — set a supported price or an explicit `priceTier`.
- Store: the prices audit reads the pricing scheme from the published app submission (the add-on
  submission's flag is unreliable), removing false MISMATCH reports and negative amounts; a malformed
  local `metadata.json` is reported rather than crashing the action.

## 1.2.0

### Setup readiness
- **Unprepared-project check** — when the menu opens, it flags a project that isn't set up yet
  (dependencies not installed, an `.env` template with no `.env`, a Python project with no
  virtualenv) and offers to install dependencies. Deterministic and offline; skipped with `--yes`.
  `doctor` reports the same under a `setup:` line.

### Natural-language search
- **Ask for an action in plain words** — when a search matches nothing, the menu can map the query to
  an existing action with AI (opt-in; needs a key). Map-only: it only picks an action the project
  already defines and never synthesizes a command.

## 1.1.0

### Failure diagnosis
- **Diagnose failed actions** — when a command exits non-zero, the menu analyzes its stderr and
  explains the failure instead of just showing `exit 1`. Recognizes a missing CLI on PATH (Windows
  "is not recognized" / *nix "command not found"), unresolved modules, a missing npm script, a bound
  port (`EADDRINUSE`), and Python `ModuleNotFoundError`. Deterministic and offline.
- **Sub-directory aware** — for scripts that `cd` into a sub-app (`cd tauri-ui && npm run tauri dev`),
  the suggested fix targets the directory that actually failed, parsed from the run output.
- **Offered fixes** — installing missing dependencies is offered behind a one-key confirm, never run
  automatically. Port conflicts and Python imports are advice-only.
- Headless runs (`run`, `dev`/`build`/`test`) print the same diagnosis to stderr.

### AI assist (optional)
Off by default and opt-in. Uses a direct request to Anthropic or OpenAI, keyed from the environment
(`ANTHROPIC_API_KEY` / `OPENAI_API_KEY`) — no SDK and no bundled keys; model overridable via
`NAVIG_MENU_AI_MODEL`. Without a key, each feature falls back to a copy-paste prompt.
- **Diagnosis fallback** — for errors the built-in rules don't recognize. Interactive asks first;
  headless is behind `--ai`. Suggested commands are shown for review, never run; destructive
  one-liners are filtered out.
- **Menu curation** (`build --ai`) — improves labels, descriptions, grouping, and the project purpose
  on top of the deterministic build. Display fields only: it never changes what runs or its risk
  tier, and manual edits are preserved.
- **Pre-run safety review** — before a `dangerous` action's typed confirmation, summarizes what the
  command does and its risk. Advisory; never replaces the confirmation.

### Fixes
- The generated `menu` script uses `npx --yes navig-menu`, so `npm run menu` / `pnpm menu` /
  `yarn menu` don't stall on npx's install prompt.

## 1.0.0

First stable release. navig-menu walks into any repo, detects the stack, and builds a premium
interactive terminal menu — zero config, never reads `.env` secrets, runs commands as argv arrays
(no shell).

### Menu & rendering
- **Cosmic banner** — an accent-ruled box with stack, git branch + sha, runtime, date, packages,
  counts, and auto-detected local endpoints (web/API URLs + TLS). Every row is "show only if it
  exists" and individually toggleable.
- **Console banner style** — a compact alternative: title box + one `⎇ branch · N changed ·
  license · N commands` status line.
- **Four layouts** — `flat` (grouped sections on one page), `list` (one flat list, no headers or
  connectors), `categories` (a picker you enter/leave), and `tree` (a nested filesystem — namespaced
  scripts like `api:db:migrate` become folders-in-folders). Switchable live.
- **Dynamic, namespace-aware sections** — Development, Quality, Database, Build, Deploy,
  Desktop & Store, i18n, Assets, Operations, Misc — derived from script names (`api:migration:run`
  → Database, `webapp:dev` → Development, `assets:*` → Assets, …).
- Mathematically-aligned rows (emoji/wide-glyph aware), recents, type-to-search, risk glyphs, and a
  selectable command footer. ASCII glyphs by default so it renders identically everywhere.
- Banner counts include **docs** (`N scripts · N packages · N docs · N services`).
- **Broad environment detection** — web (Next/Nuxt/Astro/Remix/SvelteKit/Angular/Solid/Qwik/…),
  backend (NestJS/Fastify/Hono/…), desktop & mobile (Tauri/Electron/Capacitor/Expo/React Native/
  Wails), Apple/Xcode · Swift · Android · Flutter, languages (Python/Rust/Go/Ruby/Elixir/Java/Deno/
  Bun/…), game engines (Unity/Godot/Unreal), monorepo tools, and services (Kubernetes/Vercel/Netlify/
  AWS/Firebase/MongoDB/Sentry/…). All evidence-based; nested manifests (`src-tauri/`, `ios/Podfile`)
  are found at the default scan depth.

### Customization
- **Live Settings panel** (`s`) — accent, icons (emoji/unicode/ascii), density, layout, banner style,
  and every banner/row toggle, grouped under **Look / List / Banner rows / Actions** headings with a
  **live preview** and one-line help per option. Customize / plugins / regenerate / refresh / doctor /
  **clear recents** / edit are folded in. Persists to `.navig/menu.json`.
- **Menu Customizer** (`c`) — hide/show individual commands; a category with everything hidden
  disappears.
- **Curation** — `overrides` (relabel/regroup/redescribe any detected script by id) and `groups`
  (custom section title/glyph/tone + order) turn a raw repo into a hand-authored console.
- Regenerate / refresh / doctor / edit are folded into Settings; the rail is just Settings · About ·
  Quit.

### Plugins
- **Plugin system** — declarative (JSON/YAML) or programmatic (`definePlugin`) plugins, discovered
  from built-ins, `.navig/plugins/`, and npm (`navig-menu-plugin-*`). Toggle in the in-menu Plugins
  manager. Plugins can change the banner, overwrite text, add sections/commands, claim scripts, add
  settings, and run handlers — folded into the model only (the pure scan stays execution/secret-free).
- **Bundled store plugin (skeleton)** — auto-activates on store/steam projects; shows a
  `Store · <product>: <N> downloads` banner line and a STORE section.
- **Bundled ports plugin** — auto-activates on projects that serve on a port. **Free my dev ports
  (auto)** kills whatever holds this project's ports; **Auto free port → .env** finds an open port and
  writes `PORT=` into `.env`. Plus: kill a port (pick/type), list active ports, find what holds a
  port, and grab a random free port. Cross-platform (netstat + tasklist on Windows, lsof elsewhere);
  if a kill fails it surfaces the holding process. Options: range, confirm-before-kill, force-kill.
- Handler primitives for interactive plugins: `capture` (run + capture stdout), `select`, `input`,
  `confirm`.
- Public authoring APIs: `navig-menu/plugin` and `navig-menu/menu-builder`.

### About
- **About screen** (`a`) — `navig-menu vX`, https://navig.run, github.com/miztizm, loaded plugins,
  license.

### Commands
`navig-menu` (open) · `list`/`ls` · `scan` · `build`/`generate` · `setup` · `doctor` · `run <action>`
· `--json`. Flags: `--cwd` · `--deep` · `--plain` · `--yes` · `--relay` · `--host` · `--no-cache`.
