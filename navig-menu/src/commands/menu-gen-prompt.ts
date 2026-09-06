/**
 * The "Ultimate Menu" authoring prompt, emitted by `navig-menu build --ai` so any external AI
 * (Claude Code, or NAVIG's operator) can author `.navig/menu.json`. Kept in sync with the
 * default `.navig/brain/prompts/menu-gen.md` shipped on the NAVIG side.
 */
export const MENU_GEN_PROMPT = `You are authoring a menu definition (.navig/menu.json) for the repository in the current working directory. Scan only — do not run installs, builds, deploys, migrations, formatters, or generators; do not edit package.json scripts.

1. Detect (evidence-based): package manager (priority: packageManager field → lockfile → workspace config → install metadata; flag multiple-lockfile conflicts); workspace shape (pnpm/npm-workspaces/turbo/nx/pseudo/none/nested); languages; frameworks (Next/Vite/React/Vue/Nuxt/Astro/Svelte/Tauri/Electron/Laravel/Python/Rust); services (Stripe = SDK dep AND webhook route; Cloudflare = wrangler; Docker = compose; Supabase/Prisma/Drizzle; Redis). For .env* capture filenames and variable NAMES only — never values.
2. Classify every real script into Launch / Quality / Build & Release / Data & Services / Operations / Utilities, and map canonical actions (dev, build, test, typecheck, lint, deploy, migrate, seed) onto the repo's actual script names. Preserve every original script.
3. Tag risk: safe (dev/lint/test/typecheck) · confirm (build/migrate/seed) · dangerous (deploy/reset/clean/destructive-db).
4. Emit .navig/menu.json following the published schema (menu.navig.run/schema/v1.json): set title, purpose, accent; an actions map (canonical → command); optional hide[], extra[], and ui. Every override action may carry label, description, risk, longRunning, group, why. Set ui.recentLimit, ui.showDescriptions, ui.showCommandHints, and ui.footer when useful. Do not invent scripts that don't exist.
5. Stats must be factual — no fabricated health score. Service lines read like "Stripe: SDK detected · webhook route found · no secrets inspected".

Deliver the .navig/menu.json plus a short audit table and any detection limitations. Never assume stack from folder names.`;
