# The built-in `env-doctor` plugin

Keeps your local environment in sync with the project's **example** env file. It auto-activates when
the project ships one, shows how many documented keys are still missing from your real env, and can
scaffold the gaps — working purely on variable **names**.

> **Names only, never values.** env-doctor reads env files solely to compare *key names*. A value is
> never captured, printed, logged, or written — the scaffold adds empty `KEY=` lines for you to fill
> in. (This is the plugin-system ground rule for anything touching `.env`.)

## Activation

Turns on automatically (toggle it in the Plugins manager, key `p`) when the project root has an
example-env file — any of:

- `.env.example` · `.env.local.example` · `.env.<anything>.example`
- `.env.sample` / `.env.template`
- **`.dev.vars.example`** (the Cloudflare Workers secrets convention)

Each example is paired with the real file it documents by stripping the suffix
(`.env.example`→`.env`, `.dev.vars.example`→`.dev.vars`, `.env.local.example`→`.env.local`).

## The palette

An **Env** rail:

| Action | What it does | Risk |
| --- | --- | --- |
| **Check env keys** | for every example→target pair, lists which documented keys are **present** vs **missing** (and any extra keys your env has that the example doesn't). Read-only, names only. | safe |
| **Scaffold missing keys** | appends empty `KEY=` lines for the missing keys to the target env file (creating it if needed) — after showing you the list and confirming. You fill in the values. | confirm |

### Banner

```
env · 2 keys missing        (yellow — you still need to set some)
env · all keys set          (green — your env matches the example)
```

The count is summed across every example→target pair, read offline (names only).

## Settings

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `banner` | toggle | on | show the `env · N keys missing` stat line |

## Why

Onboarding a repo (or a fresh machine) usually means "the app crashes because an env var isn't set."
env-doctor turns that into one glance (the banner) and one action (scaffold the missing keys), without
ever exposing a secret value.

## Internals

```
src/plugins/builtin/env-doctor/
  index.ts          plugin def — detect · contribute · handlers
  config.ts         findEnvPairs (example→target discovery) + auditEnv (missing/extra by name)
  banner.ts         env · N keys missing / all set
  handlers.ts       check (read-only audit) + scaffold (confirmed append of empty KEY= lines)
  engine/
    dotenv.ts       parseEnvKeys (NAMES only) + buildEnvAppend (empty KEY= lines) — pure, unit-tested
```

Tests: `test/env-doctor.test.ts` — key-name parsing (incl. an explicit "a value never leaks" case),
pair discovery, audit (missing/extra/target-missing), banner, and detect/contribute over a temp repo.
