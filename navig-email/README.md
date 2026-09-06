# navig-email

NAVIG Email module — Gmail accounts, filter→notify triage, scheduled briefings & send. A first-party navig plugin (free, toggleable).

**License:** Apache-2.0 · **Version:** 0.1.0

## What it is

`navig-email` is a **first-party NAVIG plugin** — it extends NAVIG itself (CLI verbs +
gateway routes), and it is free and toggleable. Plugins are different from
*blocks*: a plugin adds capabilities to the platform; a block is an outcome you
*apply* with `navig apply`.

## Install

It ships in the NAVIG monorepo and loads automatically when NAVIG is installed.
To add/enable it explicitly:

```
navig plugin add navig-email
```

Check wiring and toggle state:

```
navig store list
```

## Development

This package lives in the NAVIG monorepo under `plugins/navig-email/`. It registers via
the `navig.plugins` entry-point group; CLI verbs register via `navig.commands`.
