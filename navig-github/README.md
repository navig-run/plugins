# navig-github

NAVIG GitHub module — the **full GitHub toolbox** (backup, mirror, export, restore,
analytics, scheduling) wired natively into `navig github`. A first-party navig plugin
(free, toggleable).

**License:** Apache-2.0 · **Version:** 0.2.0

## What it is

`navig-github` is a **first-party NAVIG plugin** — it extends NAVIG itself (CLI verbs +
gateway routes), and it is free and toggleable. Plugins are different from
*blocks*: a plugin adds capabilities to the platform; a block is an outcome you
*apply* with `navig apply`.

The GitHub engine is bundled in-package (`navig_github.engine`) and
mounted **in-process**: every engine command is a `navig github` subcommand, and a
group callback injects the vault token (`github_token`) into `GITHUB_TOKEN` so the
whole surface authenticates automatically.

```
navig github token set <PAT>          # store the token in the navig vault, once
navig github backup <user> -y         # mirror every repo (curated wrapper)
navig github issues owner/repo        # export issues (in-process)
navig github releases owner/repo      # download releases + assets
navig github verify ./mirrors         # backup integrity check
navig github --help                   # the full surface, grouped by panels
```

## Install

It ships in the NAVIG monorepo and loads automatically when NAVIG is installed.
To add/enable it explicitly:

```
navig plugin add navig-github
```

Check wiring and toggle state:

```
navig store list
```

## Development

This package lives in the NAVIG monorepo under `plugins/navig-github/`. It registers via
the `navig.plugins` entry-point group; CLI verbs register via `navig.commands`.
