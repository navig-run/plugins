# AGENTS.md — navig-menu

Guidance for AI agents (Claude Code, NAVIG operator, Cursor, Copilot) working in this repo. This
file is the source of truth; keep `.github/instructions/` in sync with it.

## What this is

A standalone TypeScript engine that **builds** interactive terminal menus from project detection. It
ships as `navig-menu` (npx) and as a Bun-compiled single binary, and integrates into NAVIG as
`navig menu`. The architecture is a pipeline: **sources → manifest → builder → menu definition → UI →
runner**.

## Layout

- `src/detectors/` — pure, evidence-based detection (package manager, workspace, frameworks, services).
- `src/sources/` — pluggable menu sources. `project-scripts.ts` is complete; `system-cli.ts` and
  `installed-apps.ts` are v1.1 stubs implementing the same `MenuSource` interface.
- `src/builder/` — `canonical` (action mapping), `risk` (tiers), `classify` (groups), `merge`
  (override precedence), `build` (orchestration + cache).
- `src/manifest/` — zod schema + fingerprint + `.navig` paths.
- `src/ui/` — the reusable framework: `theme`, `banner`/`cosmic`, `prompts` (enquirer adapter),
  `confirm`, `menu` (the TUI loop + Settings/Customizer/Plugins/About views), `menu-builder` (the DSL).
- `src/plugins/` — the plugin system: `types` (the `MenuPlugin` API + `definePlugin`), `manifest`
  (declarative JSON/YAML), `loader` (built-in/local/npm discovery + isolation), `apply` (fold
  contributions into the MODEL), `builtin/` (bundled `example` + `store` skeleton). Plugins never
  touch the manifest.
- `src/runners/` — `local` (execa, argv array) and `relay` (emit action-request JSON for NAVIG).
- `src/commands/` + `src/cli.ts` — command implementations and the arg dispatcher.

## Non-negotiable rules

1. **No execution during detection.** Detectors only read files. Never run installs/builds to detect.
2. **Never read or print secret values.** `.env*` yields filenames and variable NAMES only. The
   `secret-safety` test asserts no value ever reaches the manifest — keep it green.
3. **Argv arrays, never shell strings.** Spawn `launcher` + `argv[]`. No `shell: true` in the binary.
4. **Typed dangerous confirmation.** `--yes` skips the `confirm` tier but NEVER the typed `dangerous`
   prompt. Only an explicit `--force-dangerous` may.
5. **Never rewrite `package.json` scripts.** The single consented exception is *adding* a `menu`
   script in `setup`, with confirmation.
6. **Overrides win, edits survive.** `.navig/menu.json` is the human/AI contract; `build` merges and
   preserves it. `.navig/menu.cache.json` is generated + gitignored.
7. **Cross-platform.** Resolve launchers via `util/which.ts` (PATHEXT on Windows). Honor `NO_COLOR`,
   `--plain`, and non-TTY (machine-readable output, no prompt noise).
8. **Plugins run only in the model/menu phase.** The pure scan never imports/executes plugin code;
   `buildMenuModel` folds contributions in. Declarative plugins are data-only. `detect`/`contribute`
   are pure (read-only context); the only sanctioned spawn is `PluginActionContext.run` (argv, no
   shell). A broken plugin is isolated to a warning — it must never crash the menu.

## Build / test

```bash
npm run typecheck   # tsc --noEmit
npm test            # vitest run (fixtures under test/fixtures/)
npm run build       # tsup → dist/cli.js
npm run binaries    # bun --compile matrix → out/
```

## Execution doctrine

Operate as a high-agency staff engineer: role-up, pick the strongest option, implement like an owner
(reuse before adding, root-cause not symptom), and **verify — don't assume** (run it). Escalate only
on product-critical forks, each with a recommended default.
