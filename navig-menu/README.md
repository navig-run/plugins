# navig-menu

**An AI-first menu builder for any project.** Walk into a repo, run one command, and `navig-menu`
auto-detects its stack, package manager, scripts, workspace shape, frameworks and services — then
builds a premium interactive terminal menu with a factual banner and safe, one-keystroke actions. No
per-project `menu.js` to hand-write.

```bash
npx navig-menu
```

That's it — run it in any project root, zero install, nothing to configure. It also runs as a single
self-contained binary (no Node), and integrates into [NAVIG](https://navig.run) as `navig menu`.

```
──────────────────────────────────────────────────────────
│  🌌  S C H E M A  ·  developer console
──────────────────────────────────────────────────────────
│  🔭  webapp     https://localhost   TLS ✓
│  ⚡  api        http://localhost:3001
──────────────────────────────────────────────────────────
│  🧩  stack      pnpm · Next.js · Cloudflare · Stripe
│  🌿  branch     main  @a1b2c3d  · clean
│  🖥  node       v22 · linux x64
──────────────────────────────────────────────────────────
│  📊  42 scripts · 3 packages · 2 services
──────────────────────────────────────────────────────────

✦ What would you like to do? …

⚡ DEVELOPMENT 4 ──────────────────────
> ◆ Dev                            pnpm run dev
  ◆ Preview                        pnpm run preview
🧪 QUALITY 9 ──────────────────────────
  ◆ Test                           pnpm run test
  ◆ Lint                           pnpm run lint
🧰 UTILITIES 3 ────────────────────────
  ⚙ Settings                       accent · layout · plugins · …
  ★ About                          navig.run · @miztizm
  ⏻ Quit

↑/↓ move · enter run · / search · s settings · c customize · q quit
```

> Prefer ASCII? It's the default (renders identically on every terminal). Switch to emoji/unicode,
> the compact **console** banner, or the **tree** (nested filesystem) / **list** / **categories**
> layouts live in Settings (`s`).

## Install / run

```bash
npx navig-menu          # zero-install, in any project root
# or, with the global binary:
npm i -g navig-menu && navig-menu
# or, inside NAVIG:
navig menu
```

Want `npm run menu` / `pnpm menu` / `yarn menu` to open it? Run `navig-menu generate` — it writes
`.navig/menu.json` and adds a single `"menu": "npx --yes navig-menu"` script when `package.json`
has no menu script yet. It always adds `"menu:navig": "npx --yes navig-menu"` too, so generated
menus have one stable command across projects. If a project already has a custom `menu` script, it is
preserved and only `menu:navig` is added. (`npm menu` without `run` is not possible — npm has no
custom-command support — but `pnpm`/`yarn` run scripts without `run`, and the global `navig-menu`
binary works anywhere.)

## Commands

| Command | What it does |
| --- | --- |
| `navig-menu` | Open the menu (auto-builds on first run). |
| `navig-menu build [--ai]` | (Re)generate `.navig/menu.json` from detection. `--ai` curates labels/descriptions/grouping when a key is set (else emits an authoring prompt). |
| `navig-menu generate` | Create the same pro `.navig/menu.json` contract and add `npm run menu` (`gen` and `create` are aliases). |
| `navig-menu list` | Print a static Schema-style menu preview (`ls` and `preview` are aliases). |
| `navig-menu import <file>` | Import a curated command catalog into `.navig/menu.json` (`--write` to apply). |
| `navig-menu organize` | AI: describe every command + report gaps (`--write`; needs a key, else emits a prompt). |
| `navig-menu scan` | Detect + report (refresh cache), no UI. |
| `navig-menu setup` | Guided config; offers the `menu` npm script (uses the repo's package manager). |
| `navig-menu doctor [--fix]` | Diagnose environment + detection. `--fix` repairs (missing scripts / orphaned overrides), confirm-gated, with a `package.json` backup. |
| `navig-menu run <action>` | Run a canonical action (`dev`/`build`/`test`/…) or any script id. |
| `navig-menu --json` | Emit the machine manifest (no UI) — for tooling / CI. |
| `navig-menu list --json` | Emit the resolved menu model — a palette-ready `{ title, purpose, groups[{ group, items[{ id, label, command, risk, description }] }] }`. |

Flags: `--cwd <path>` · `--deep` · `--plain` · `--yes` · `--write` · `--fix` · `--relay` · `--host <name>` · `--no-cache`.

### Programmatic API

`navig-menu list --json` is the stable palette payload for other tools (VS Code, a GUI command palette). For in-process use without shelling out, import the resolver from **`navig-menu/model`**:

```ts
import { scanProject, loadDefinition, buildMenuModel } from "navig-menu/model";
const model = buildMenuModel(scanProject(root), loadDefinition(root));
// model.groups[].items[] → { id, label, launcher, argv, cwd, risk, description, project, … }
```

(`navig-menu/menu-builder` is the *authoring* DSL; `navig-menu/model` is the *resolver*.)

## How it works

```
sources (detect, no execution)  →  manifest (evidence + confidence)  →  builder (heuristic + AI)
   project scripts                   .navig/menu.cache.json (gitignored)     canonical map + risk
                                                                                     │
runner (local | NAVIG relay)  ←  UI framework (banner + flow + DSL)  ←  .navig/menu.json (versioned)
```

- **Evidence-based detection.** Every framework/service carries an `evidence[]` array and a
  `confidence` (`explicit`/`detected`/`inferred`/`unknown`). It **never reads `.env` values** — only
  filenames and variable names.
- **Canonical actions, not rewritten scripts.** `dev`/`build`/`test`/… are mapped onto whatever your
  repo actually calls them. Your `package.json` scripts are never rewritten.
- **Safety tiers.** `safe` runs directly; `confirm` asks once; `dangerous` (deploy/reset/clean/
  destructive-db) needs a **typed** confirmation that `--yes` cannot bypass. Commands run as an argv
  **array** — never a shell string.
- **Human/AI overrides.** Edit `.navig/menu.json` (or let an AI author it). Re-builds **merge** and
  preserve your edits.
- **Pro generator.** `navig-menu generate` writes a rich contract with canonical action objects,
  descriptions, risk tiers, lifecycle-script hiding, UI defaults, and an audit of mapped versus
  preserved entries. It also adds the safe zero-install `menu` package script if missing.
  `navig-menu generate --json` previews the file without writing.
- **Stable pro TUI.** The menu is a fixed-height command palette with recents, grouped sections,
  type-to-search, details, and refresh. Keyboard movement is clamped to the visible viewport, so
  short terminals do not corrupt selection state.
- **Static preview.** `navig-menu list --plain` prints the same banner, sections, selected-command
  footer, and scroll affordances without opening raw TTY mode. Use it for screenshots, CI checks, or
  quick command discovery.
- **Local history.** Recently launched commands are stored in `.navig/menu.recent.json` and ignored
  by git. The versioned `.navig/menu.json` can tune UI behavior with `ui.recentLimit`,
  `ui.showDescriptions`, `ui.showCommandHints`, and `ui.footer`.

See [`docs/`](docs/) for the DSL, the override schema, and the AI-authoring contract.

## Failure diagnosis

When an action exits non-zero, the menu analyzes its output and explains the failure instead of just
showing `exit 1`. It recognizes a missing CLI on PATH (Windows "is not recognized" / *nix "command
not found"), unresolved modules, a missing npm script, a bound port, and Python `ModuleNotFoundError`
— deterministically and offline. For scripts that `cd` into a sub-app before running, the suggested
fix targets the directory that actually failed. Installing missing dependencies is offered behind a
one-key confirm and never runs automatically; other cases are advice-only.

## Setup readiness

When you open the menu, it checks whether the project is ready to run — dependencies installed, an
`.env` present when a template exists, a Python virtualenv when there are Python requirements — and,
if not, offers to install dependencies before you start. Deterministic and offline; `doctor` reports
the same. Skip it with `--yes`.

## AI assist (optional)

Off by default and opt-in. When a provider key is set in the environment (`ANTHROPIC_API_KEY` or
`OPENAI_API_KEY`; model via `NAVIG_MENU_AI_MODEL`), three features become available — with no bundled
SDK or keys, and a copy-paste prompt fallback when no key is present:

- **Diagnosis fallback** for failures the built-in rules don't recognize (interactive asks first;
  headless is behind `--ai`). Suggested commands are shown for review, never run.
- **Menu curation** (`build --ai`) — improves labels, descriptions, grouping, and the project purpose
  on top of the deterministic build. Display fields only: it never changes what runs or its risk
  tier, and your manual edits are preserved.
- **Pre-run safety review** — before a `dangerous` action's typed confirmation, a plain-language
  summary of what the command does and its risk. Advisory; it never replaces the confirmation.
- **Natural-language search** — when a search matches nothing, map the query to an existing action
  ("start the frontend" → Dev). Map-only: it only picks an action the project already defines.

## Plugins

The menu is extensible. A plugin can change the banner (add lines/phrases), overwrite text
(title/tagline/prompt), add sections + commands, claim/relabel your scripts, add settings, and expose
actions with handlers. Two tiers: **declarative** (a safe JSON/YAML file in `.navig/plugins/`) and
**programmatic** (`definePlugin({...})` for dynamic work). Plugins are discovered from built-ins,
`.navig/plugins/`, and npm (`navig-menu-plugin-*`), and toggled from the in-menu **Plugins** manager
(key `p`). Bundled plugins include **store** (Microsoft Store publishing), **wrangler** (a
Cloudflare/Wrangler palette — deploy · dev · tail · D1 · KV · R2 · secrets — that auto-activates in
any Workers/Pages project and reads its `wrangler` config; see
[`docs/wrangler-plugin.md`](docs/wrangler-plugin.md)), **github** (a `gh`-CLI palette — PRs · issues ·
Actions/CI · releases — for any repo with a GitHub remote; see
[`docs/github-plugin.md`](docs/github-plugin.md)), **env-doctor** (missing-env-key auditing — names
only; see [`docs/env-doctor-plugin.md`](docs/env-doctor-plugin.md)), and **ports**. Full guide:
[`docs/plugins.md`](docs/plugins.md).

Plugins run only in the model/menu phase — never during the pure detection scan, so the cached
manifest stays execution-free and secret-free.

## About

Press `a` for the **About** screen — tool version, website ([navig.run](https://navig.run)), author
([github.com/miztizm](https://github.com/miztizm)), and the loaded plugins.

## License

MIT
