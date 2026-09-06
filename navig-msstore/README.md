# navig-msstore

`navig mstore` — the full Microsoft Store toolkit for NAVIG.

A **standalone first-party plugin** (lives in `plugins/`, **not** in core). It is the
single source of truth for Store operations — configure identity, resolve credentials,
package the MSIX, publish, and check status — for **any** app (via `--dir`). It wraps the
same `msstore-publish` **block** (so publishing stays verifiable) without bloating core.

## How it mounts (no core edit)

NAVIG's CLI auto-mounts every `navig.commands` entry point as a top-level `navig <verb>`
(`core/navig/cli/registration.py`). This package declares:

```toml
[project.entry-points."navig.commands"]
mstore = "navig_msstore:app"
```

so once installed, `navig mstore` exists. Disable it like any plugin
(`navig plugin disable navig-msstore`) — degrade-never-blocks-boot.

## Install

```
pip install -e plugins/navig-msstore      # from the repo root
navig mstore --help
```

## Credentials (never hardcoded)

`msstore` authenticates with an Azure AD service principal. This plugin resolves each
value with a deliberate precedence:

1. **explicit env** — `NAVIG_MSSTORE_TENANT_ID` / `_CLIENT_ID` / `_CLIENT_SECRET` (a per-tool override)
2. **the vault** — provider `partner_center` (the Microsoft Partner Center App-Only credential
   from the navig-harbor connector), then `azure`; profile `connector`, then `default`. Read via
   the official `navig vault get <provider>/<field> --raw` seam, which handles the
   `provider/data_key` path **and** the credential profile.
3. **generic Azure env** — `AZURE_TENANT_ID` / `_CLIENT_ID` / `_CLIENT_SECRET`, **last on purpose**:
   `AZURE_*` is shared by az-cli / Terraform / etc. and may be a *different* Azure app, so the
   deliberate vault creds win over it.

```
navig mstore creds     # what resolves + from where (partner_center@connector) -- never reveals a value
navig mstore auth      # resolve -> msstore reconfigure
```

Vault fields expected: `partner_center/tenant_id`, `partner_center/client_id`,
`partner_center/client_secret` (profile `connector`). Override the first-tried source with
`NAVIG_MSSTORE_VAULT_PROVIDER` / `NAVIG_MSSTORE_VAULT_PROFILE`. `creds` shows only the source
(no values); `auth` reveals via `--raw` only to hand the secret to `msstore` once, never logging it.

## Verbs (every one accepts `--dir <app>`, default: cwd)

| Verb | What it does |
|---|---|
| `configure` | write `store/identity.json` (Store Product ID + MSIX identity) — interactive |
| `auth` | resolve creds (env/vault) → `msstore reconfigure` |
| `creds` | show what resolves + from where (never reveals a value) |
| `package` | build the MSIX (drives the app's `scripts/create-msix.ps1`; `--build` tauri-builds first, `--dry-run` stages) |
| `publish` | apply the `msstore-publish` block (auto product_id from identity.json + latest `.msix`) |
| `status` | `msstore submission status` |
| `show` | identity + latest built MSIX |
| `open` | Partner Center dashboard |
| `doctor` | preflight the whole chain — tooling (msstore, MakeAppx), creds, block, and per-app readiness (`--dir`) |

```
# full flow for an app (Anchor shown; -C/--dir points at any app)
navig mstore configure -C apps/anchor      # once, from Partner Center
navig mstore auth                          # creds -> msstore
navig mstore package  -C apps/anchor --build
navig mstore publish  -C apps/anchor       # auto product_id + latest .msix
navig mstore status   -C apps/anchor
```

`publish` stays sugar over `navig apply msstore-publish … --approve publish` — the block does
the real, receipt-backed, `--approve`-gated work.

## The three surfaces

| Surface | Role |
|---|---|
| **Block** `msstore-publish` | the portable, verifiable publish primitive (receipt) |
| **Plugin** `navig mstore` | the operator surface — all Store logic; wraps the block + workers |
| **Menu** `npm run menu` (per app) | a **thin interactive front-end** that just calls `navig mstore` verbs |

Core stays clean — none of this is a hardcoded core command.
