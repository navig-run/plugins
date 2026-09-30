# Plugin ideas & roadmap

A running list of plugins worth building for `navig-menu`. Each one follows the same shape as the
bundled `ports` / `store` plugins: a `detect()` predicate, optional **banner line(s)**, a **section**
of commands, and **handlers** for the interactive bits. See [`plugins.md`](plugins.md) for the API.

Ground rules every plugin here must respect (from `AGENTS.md`):
- Plugins run **only in the model/menu phase** — never during the pure detection scan.
- `.env` gives **variable names only**, never values. Credentials are surfaced as *names to set*.
- Commands run as **argv arrays, no shell**. Dangerous actions carry `risk: "dangerous"`.
- Network calls happen **only in handlers** (e.g. "Refresh stats"), results **cached** to
  `.navig/plugins-cache/<id>/…`, and the banner reads the cache — never blocks the menu.

---

## Publishing plugins

> ✅ **Shipped:** the Microsoft Store pipeline now lives in the bundled `store` plugin (native
> Partner Center engine, driven by `store.config.json`) — see [`store-plugin.md`](store-plugin.md).
> The Steam side (`steamcmd` + VDF staging) remains open as a future plugin.

### `npm-publish`
Detects a publishable npm package.

| | |
| --- | --- |
| **Detect** | `package.json` with `name` + `version` and not `"private": true`, or a `publish`/`release` script |
| **Banner** | `npm · <name>@<version> · <N> weekly downloads` (npm registry API, cached) |
| **Section** | Dry-run publish · Publish (`--access public`) · Version bump (patch/minor/major) · Dist-tags · `whoami` / login check · Check name availability |
| **Handlers** | `npm publish --dry-run`, `npm version …`, `npm dist-tag …` (argv, no shell); publish is `risk: "dangerous"` |
| **Creds** | none beyond `npm login` (surfaces "run `npm login`" if `whoami` fails) |

### `chrome-extension` (Web Store)
Detects a browser extension.

| | |
| --- | --- |
| **Detect** | a `manifest.json` with `manifest_version` (+ optional `.crx`/`web-ext` config) |
| **Banner** | `Chrome Web Store · <name> v<ver> · <users> users · ★<rating>` (Web Store API, cached) |
| **Section** | Build zip · Bump version · Upload · Publish · Submission status · Validate manifest · (siblings: Firefox AMO, Edge Add-ons) |
| **Handlers** | `chrome-webstore-upload-cli` / `web-ext`; publish is `dangerous` |
| **Creds** | `CHROME_CLIENT_ID` / `CHROME_CLIENT_SECRET` / `CHROME_REFRESH_TOKEN` (names only) |

### `app-store` / `play-store` (Fastlane)
| **Detect** | `fastlane/Fastfile`, or an `apple`/`android` project (see detection below) |
| **Banner** | `App Store · v<ver> · <status>` / `Play · <track> · <status>` |
| **Section** | Build · Beta (TestFlight / internal) · Release · Screenshots · Metadata sync |
| **Creds** | `APP_STORE_CONNECT_*` / `PLAY_SERVICE_ACCOUNT_JSON` path (names only) |

### Registry siblings — `pypi` · `crates` · `homebrew` · `winget`/`scoop`
Small variants of `npm-publish`, one per registry:
- **pypi** — detect `pyproject.toml` with a `[build-system]` → `uv publish` / `twine upload`.
- **crates** — detect `Cargo.toml` `[package]` → `cargo publish` (dry-run first).
- **homebrew / winget / scoop** — detect a formula / manifest → bump + PR the version.

---

## "Data banner" plugins

### `api-data-banner` (generic)
The most-requested, zero-code way to put a live number in the banner.

| | |
| --- | --- |
| **Detect** | always available; configured per project in `.navig/plugins/*.json` |
| **Config** | `{ url, jsonPath, label, tone, ttl }` — fetch `url`, read `jsonPath` out of the JSON, render `label: <value>` |
| **Banner** | e.g. `Live · 1,204 users`, `Uptime · 99.98%`, `MRR · $4.2k` |
| **Handlers** | Refresh (fetches + caches; respects `ttl`); never blocks the menu — banner reads the last cache |

Example config:
```json
{ "id": "live-users", "url": "https://api.acme.com/stats", "jsonPath": "data.activeUsers",
  "label": "Live users", "tone": "green", "ttl": 300 }
```

### `github` — ✅ shipped
Shipped as the built-in **`github`** plugin (`gh`-CLI-driven: PRs · issues · Actions/CI · releases ·
repo/auth; `owner/repo` banner with cached stars / open-PR / CI). See
[`github-plugin.md`](github-plugin.md).
| **Detect** | a git repo with a GitHub remote (github.com or Enterprise), read from `.git/config` |
| **Banner** | `GitHub · <owner/repo> · ★<stars> · <open PRs> PRs · CI <status>` (via `gh` CLI, cached) |
| **Section** | Status · Pull requests · Issues · Actions/CI · Releases · Repo overview · Open on GitHub · Refresh stats · Auth |
| **Handlers** | `gh pr list`, `gh run list`, `gh release create …` (via `capture`/`run`) |

### `coverage`
| **Detect** | a `coverage/coverage-summary.json` (or `.nyc_output`) |
| **Banner** | `coverage · 87%` (green ≥ threshold, yellow below) |
| **Section** | Run coverage · Open HTML report |

### `bundle-size`
| **Detect** | a build output dir (`dist/`, `.next/`, `build/`) |
| **Banner** | `bundle · 214 kB (▲ 6 kB)` (delta vs last cached build) |
| **Section** | Analyze · Build + measure |

### `env-doctor` — ✅ shipped
Shipped as the built-in **`env-doctor`** plugin (also detects `.dev.vars.example`, the Cloudflare
Workers convention; **Check** + **Scaffold missing keys**, names only). See
[`env-doctor-plugin.md`](env-doctor-plugin.md).
| **Detect** | a `.env.example` / `.env.*.example` / `.sample` / `.template` / `.dev.vars.example` exists |
| **Banner** | `env · 2 keys missing` (compares **names** in the example vs your env, never values) |
| **Section** | Check (present/missing) · Scaffold missing keys (empty `KEY=` lines, confirmed) |

---

## Ops / infra plugins

- **`docker`** — detect Dockerfile / compose → build · up · down · logs · ps · prune. Banner:
  `Docker · <N> services · <running> up`.
- **`deploy`** (Vercel / Netlify / Cloudflare) — detect the platform config → deploy preview / prod
  (`dangerous`), env pull (names only), logs, domains. Banner: last deploy status + URL.
  → **Cloudflare is shipped** as the built-in **`wrangler`** plugin (Workers & Pages: deploy · dev ·
  tail · D1 · KV · R2 · secrets · Queues / Vectorize / Hyperdrive · types · auth). See
  [`wrangler-plugin.md`](wrangler-plugin.md). Vercel/Netlify remain open.
- **`db`** (Prisma / Drizzle / Supabase) — migrate · studio · seed · **reset** (`dangerous`). Banner:
  `DB · <provider> · <N> migrations · <M> pending`.
- **`release`** (Changesets / semantic-release) — add changeset · version · publish · changelog.
- **`i18n`** — detect locale files → extract · sync · report missing. Banner:
  `i18n · <N> locales · <M>% translated`.
- **`e2e`** (Playwright / Cypress) — run · UI mode · report · codegen.
- **`status`** — ping a healthcheck URL → banner `● up` / `● down`, cached.

---

## Ultimate environment detection (roadmap)

`navig-menu` already detects a wide stack (shipped in 1.0):

- **Web**: Next.js, Nuxt, Astro, Remix, SvelteKit, Gatsby, Angular, RedwoodJS, Docusaurus, VitePress,
  Svelte, Solid, Qwik, Preact, Vue, Vite, React.
- **Backend**: NestJS, Fastify, Koa, AdonisJS, Elysia, Express, Hono.
- **Desktop / mobile**: Tauri, Electron, Neutralino, Capacitor, Expo, React Native, Wails.
- **Apple / native**: Apple/Xcode (`.xcodeproj` / `Podfile` / `Info.plist`), Swift (`Package.swift`),
  Android (`AndroidManifest.xml`), Flutter (`pubspec.yaml`).
- **Languages / runtimes**: Python, Rust, Go, Ruby, Elixir, Java/Gradle, CMake, Zig, Deno, Bun, PHP,
  Laravel, .NET.
- **Game engines**: Unity, Godot, Unreal.
- **Monorepo**: Turborepo, Nx, pnpm/yarn/npm workspaces.
- **Services**: Stripe, Cloudflare, Docker, Kubernetes, Vercel, Netlify, AWS, Firebase, Supabase,
  Prisma, Drizzle, MongoDB, MySQL, Redis, Postgres, PlanetScale, Turso, Sentry.

Planned deepening (different cases the detector should learn):

1. **Per-workspace stacks** — in a monorepo, detect each package's own stack and show it in the tree
   (e.g. `apps/web → Next.js`, `apps/desktop → Tauri`, `packages/api → Hono`).
2. **Runtime pinning** — `.nvmrc` / `.node-version` / `.tool-versions` / `packageManager` field /
   `engines` → show the pinned Node/Bun/Deno version and warn on mismatch.
3. **CI provider** — `.github/workflows`, `.gitlab-ci.yml`, `.circleci`, Azure Pipelines → a `github`
   / `ci` banner line with last run status.
4. **Container / cloud target** — Dockerfile base image, `fly.toml`, `render.yaml`, `app.yaml`,
   `Procfile`, `serverless.yml`, `sst.config.ts`, Terraform/Pulumi.
5. **Package registry** — where this publishes (npm scope, private registry via `.npmrc`, crates,
   PyPI) → drives the right publish plugin.
6. **Editor / tooling** — Biome vs ESLint+Prettier, Vitest vs Jest, tsconfig strictness — surfaced in
   Doctor.
7. **OS / arch awareness** — native projects: which targets are buildable on the current OS (e.g.
   iOS builds only on macOS) → grey out or annotate actions that can't run here.
8. **Confidence upgrades** — a dep-only hit stays `inferred`; a config file + a matching script
   promotes to `detected`; a running dev server (ports plugin) confirms `explicit`.

Contributions welcome — pick one, copy the `ports` plugin as a template, and open a PR.
