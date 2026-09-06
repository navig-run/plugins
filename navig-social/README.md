# navig-social

NAVIG **Social** — compose, schedule & publish across social networks. A first-party
navig plugin (free, toggleable), extracted from `navig-media`.

## What it does

- **Publish** to X/Twitter, Facebook Page, LinkedIn, Reddit, Instagram, Threads,
  Pinterest, Dev.to (and Telegram) through a common `BasePublisher` framework.
- **Fan-out** — publish one brief to many networks in a single call, with per-platform
  adaptation + UTM tagging: `navig social fan-out --file brief.json --to x,facebook,devto,telegram`.
- **Connect** — one-command OAuth: `navig social connect <network>` (vault-backed tokens).
- **Facebook Page admin** — `navig facebook` (`fb`): check/info/set-about/photos/backup/caption/delete.
- **Studio** — the deck composer + scheduler (`/api/deck/studio/*`), fed by the background
  `ScheduledPostService`.

## Commands

```
navig social status [--check]            # connected networks
navig social connect <network>           # OAuth connect (vault-backed)
navig social fan-out --file brief.json --to x,facebook,devto,telegram [--dry-run]
navig social stats <platform> <handle>   # PUBLIC follower/view counts for ANY handle (no account link)
navig facebook check | info | photos | backup | caption | delete   (alias: navig fb)
```

## Install

Bundled with a full `navig` install (the `social` extra); standalone:
`navig store install pip:navig-social` — or `pip install navig-social` once published.
Requires `navig-core` in the same environment.

Credentials live in the NAVIG vault (`navig vault`) + config `adapters.social.<network>.*`.
Tokens are never printed.
