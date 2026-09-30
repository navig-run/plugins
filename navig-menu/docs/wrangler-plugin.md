# The built-in `wrangler` plugin

A **Cloudflare / Wrangler command palette** for any Workers or Pages project. It auto-activates when
it detects Cloudflare, parses the project's own `wrangler` config **offline** to learn what the
project actually declares, and drives the real `wrangler` CLI for you — deploy, dev, tail, D1, KV,
R2, secrets, and more — with production-safe confirmations.

It carries **zero** project-specific knowledge: everything it offers is derived from your
`wrangler.toml` / `wrangler.jsonc` / `wrangler.json` and your `package.json` scripts.

## Activation

The plugin turns on automatically (toggle it in the Plugins manager, key `p`) when a project has any
of:

- a `wrangler.toml`, `wrangler.jsonc`, or `wrangler.json`, **or**
- a `wrangler` / `@cloudflare/*` dependency, **or**
- a `package.json` script that invokes `wrangler` or `opennextjs-cloudflare`.

## What it reads

`loadWranglerConfig()` finds the first of `wrangler.toml` → `wrangler.jsonc` → `wrangler.json`, parses
it (TOML via `smol-toml`; JSONC via a small string-aware comment/trailing-comma stripper), and
normalizes it into a `WranglerConfig`: the worker/pages name, entrypoint, compatibility date/flags,
`[env.*]` environments, and every declared binding group — D1, KV, R2, Durable Objects, Queues,
Workers AI, Vectorize, Hyperdrive, service bindings, `[assets]`, and `send_email`.

The parse is pure and read-only; it never runs anything and never reads a secret value.

## Worker vs Pages

`classifyType()` decides the deploy flavour: a `pages_build_output_dir` (or a `wrangler pages …`
script with no worker `main`) means **Pages** (`wrangler pages dev/deploy …`); otherwise it's a
**Worker** (`wrangler dev/deploy …`). A Worker with `[assets]` (Workers Static Assets) stays a
Worker.

## The palette

A **Cloudflare** rail whose actions are **config-aware** — binding rails only appear when the project
declares them (or references them in a script):

| Action | What it does | Risk |
| --- | --- | --- |
| **Dev** | `wrangler dev` / `wrangler pages dev <dir>` (env picker for Workers) | safe |
| **Deploy** | `wrangler deploy` / `wrangler pages deploy <dir>` — asks the environment | dangerous |
| **Tail logs** | `wrangler tail` / `wrangler pages deployment tail` | safe |
| **Deployments / rollback** | list deployments & versions · roll back (confirmed) | confirm |
| **D1 database…** | pick a DB → query · execute a `.sql` file · migrations (create/list/apply **local** or **remote**) · export | confirm |
| **KV storage…** | namespaces list/create · per-binding key list/get/put/delete (`local`/`remote`) | confirm |
| **R2 storage…** | buckets list/create · object upload/download/delete | confirm |
| **Secrets…** | list / put / delete (Workers or Pages variant) — **values never leave wrangler** | confirm |
| **Queues / Vectorize / Hyperdrive** | shown only when declared — list/create | confirm/safe |
| **Generate types** | `wrangler types` | safe |
| **Who am I / Login** | `wrangler whoami` (caches the account for the banner) · `wrangler login` | safe |

Your project's own `wrangler`-invoking `package.json` scripts keep their normal place (the **DEPLOY**
/ scripts rails) — the generated `cf.*` actions above already cover dev/deploy/tail with
env-awareness and confirmation, so the plugin deliberately does **not** also pull those scripts into
the Cloudflare rail (that would show a bare "Deploy" next to the richer "Deploy").

### Banner

With the banner setting on, a stat line summarizes the config, e.g.:

```
Cloudflare · navig-api · worker · D1×1 · KV×1 · R2×1 · AI · Vectorize×1 · env: production/staging
```

Turn on the `whoami` setting to add a cached "signed in as …" line (populated by **Who am I** /
**Login**; no network call happens in the banner itself).

## Safety

- Every action runs wrangler as an **argv array — no shell**, so a database name, SQL string, or key
  can never be reinterpreted as a shell token.
- Production/`--remote` operations (deploy, D1 `--remote`, KV/R2 writes & deletes, rollback,
  secret delete) **confirm explicitly** in the handler — internal plugin actions bypass the menu's
  own risk gate, so the guard lives here. The `Cloudflare · confirm before production/remote ops`
  setting (default on) can be turned off for a faster loop.
- `local` vs `remote` is always explicit in D1 labels; local is the safe default.
- Secret **values** are never handled or logged: `wrangler secret put` reads the value on its own
  stdin (inherited); only the secret **name** ever passes through the plugin.

## Settings

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `banner` | toggle | on | show the Cloudflare stat line |
| `remoteConfirm` | toggle | on | confirm before any production/remote op |
| `runner` | cycle | `auto` | how to invoke wrangler — `auto` (local `node_modules/.bin/wrangler`, else `npx --yes wrangler`), `npx`, `pnpm` (dlx), or `bun` (bunx) |
| `whoami` | toggle | off | add the signed-in account to the banner (cached) |

## Not in scope

This plugin drives the `wrangler` binary for direct/maintainer flows. It is **not** a replacement for
a project's CI or for NAVIG's own pure-Python Cloudflare deployers (`navig lighthouse` /
`navig miniapp`, which upload via the CF REST API and need no Node) — those remain the end-user
deploy path inside NAVIG.

## Internals

```
src/plugins/builtin/wrangler/
  index.ts             plugin def — detect · contribute (config-aware sections) · handlers
  config.ts            find + parse + normalize wrangler.toml/jsonc/json → WranglerConfig
  banner.ts            config-derived stat line (+ cached whoami)
  handlers.ts          interactive handlers (the only side effects: spawn wrangler, prompt, cache)
  engine/
    jsonc.ts           string-aware JSONC → JSON (comments + trailing commas)
    classify.ts        worker vs pages · binding-count summary
    command.ts         pure argv builders for every wrangler op (unit-tested)
    launcher.ts        resolve the wrangler invocation (local bin → npx/pnpm/bun)
    whoami.ts          parse `wrangler whoami` → { email, account }
```

Tests: `test/wrangler.test.ts` (config parse across all three formats, JSONC stripping,
worker/pages classification, argv builders, banner, whoami, launcher, and end-to-end
detect/contribute over a temp project).
