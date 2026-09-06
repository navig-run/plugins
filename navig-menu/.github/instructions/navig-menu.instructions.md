---
applyTo: "**"
---

# navig-menu — agent instructions

Synced from `AGENTS.md` (root). When you change one, mirror the other.

navig-menu is a standalone TypeScript engine that **builds** terminal menus from project detection:
**sources → manifest → builder → menu definition → UI → runner**. It ships as `navig-menu`
(npx) + a Bun-compiled single binary, and integrates into NAVIG as `navig menu`.

## Hard rules
1. No execution during detection — detectors only read files.
2. Never read/print secret values; `.env*` → filenames + variable NAMES only (keep `secret-safety` green).
3. Spawn argv arrays, never shell strings.
4. `--yes` skips `confirm` but never the typed `dangerous` prompt.
5. Never rewrite `package.json` scripts (the only exception: *adding* a `menu` script in `setup`, consented).
6. `.navig/menu.json` is versioned + human/AI-owned and wins; `.navig/menu.cache.json` is generated + gitignored. Re-build merges, preserving edits.
7. Cross-platform: resolve launchers via `util/which.ts`; honor `NO_COLOR` / `--plain` / non-TTY.

## Verify before claiming done
`npm run typecheck && npm test && npm run build`. Run the binary with no Node on PATH to prove zero-dep.
