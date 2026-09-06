# Changelog — navig-social

## 0.1.2 — 2026-09-01

### Fixed
- **Declares `navig>=3.24.0`.** 0.1.1 declared no dependency on navig while importing
  it at module scope, so `pip install navig-social` installed cleanly and then raised
  `ModuleNotFoundError` on first use. Only `pip install navig[social]` ever worked.
- **Studio's AI endpoint works.** `POST /studio/ai` imported `navig.llm_generate`, a
  module that has never existed; the ImportError was caught and returned 502 — on every
  request, for every action. It also passed the timeout as the 7th positional argument,
  which is `model_override`, so fixing only the import would have swapped one failure
  for another. Now `navig.llm.generate`, called by keyword.
- Ships the full Apache-2.0 licence text in the wheel.

This release also carries everything listed under *Unreleased* below.

## Unreleased

### Changed
- **One canonical source for social brand names + brand-cased CLI tables.** The
  per-network display casing (LinkedIn / YouTube / Dev.to) now lives in a single
  leaf map — `navig_social.social.labels.DISPLAY_LABELS`, read by `display_label(id)`;
  `BasePublisher.display_label` delegates to it (the per-publisher `label` overrides
  were removed). The `navig social` **status · receipts · sync · report** tables now
  render brand-cased network names ("YouTube", not "youtube") through that same map —
  the lowercase id you type (`navig social connect youtube`) is unchanged. `oauth.py`
  needed no change (its connect/refresh notes were already correct literals).

### Fixed
- **Publisher display names in the deck/OS Networks view are brand-cased.**
  `GET /api/deck/studio/networks` labeled publishers with a naive `.title()`, so
  the desktop **Social → Networks** surface shipped "Linkedin" / "Youtube" /
  "Devto". Labels now come from each publisher's canonical `label`
  (`display_label` on `BasePublisher`), rendering **LinkedIn / YouTube / Dev.to**;
  publishers without an override keep the correct `.title()` fallback (Reddit,
  Facebook, …). (The label source was subsequently centralized and the CLI tables
  brand-cased too — see **Changed** above.)

### Added
- **`navig social stats <platform> <handle>` — public follower/view counts for ANY handle.**
  Universal: no space, no folder, no OAuth-connected account. Reads only PUBLIC data — plain HTTPS
  (github, telegram, soundcloud, kick), official APIs where a key/token is present (youtube, vk,
  facebook, twitch, instagram), or a vaulted browser via `navig cdp` for login-walled sites
  (tiktok, x, linkedin, …). `navig social stats list` shows the capability matrix; `--only-public`
  skips the browser; `--json` for scripts. The engine (`navig_social.stats`) was extracted from the
  retired **navig-presence** plugin — the space-bound content-registry layer was dropped, keeping the
  reusable stats core.
- **Publish-receipt ledger — the create→publish→measure loop's durable record.** Every live
  `fan_out` (from `navig social fan-out` *and* the `navig pipeline`) now records each network's
  receipt — campaign, network, post id, **UTM'd URL**, ok/error, timestamp — to a `BaseStore`-backed
  ledger (`navig_social.social.receipts`). Recording is best-effort (a storage hiccup never breaks
  publishing) and opt-out via `fan_out(record=False)`.
- **`navig social receipts`** — view the ledger: a campaign summary (posts / ok / last) by default,
  or `--campaign <slug>` for per-network detail (status + post id + UTM'd link). `--json` for scripts.
- **Signals seam** — `receipt_to_signals_event()` shapes a receipt into a navig-signals ingest payload,
  the one call a future engagement/click wire (keyed on `utm_campaign`) posts to `/api/ingest/<source>`.
- **`navig social report` — the campaign scorecard (loop capstone).** Joins the receipt ledger
  (what you published) with the engagement ledger (the clicks it earned) into a per-network scorecard —
  published status + link + clicks + views + **CTR** — or, with no `--campaign`, a leaderboard of all
  campaigns ranked by clicks. Pure join helpers in `navig_social.social.report`
  (`campaign_scorecard` / `leaderboard`) + a new `engagement.network_metrics()` breakdown; `--json`.
- **Click-tracking redirects — the loop runs itself.** With
  `adapters.social.tracking.base_url` set (typically your lighthouse URL), a live fan-out wraps each
  click-bearing link in `<base>/r/<campaign>/<network>` (dev.to's canonical stays clean). A click hits
  the plugin's `GET /r/{campaign}/{network}` route, which resolves the real destination **from the
  receipt ledger** (never a query param → open-redirect-free), records a `clicks` engagement event,
  and 302s. Publicly reachable via lighthouse's arbitrary-path forwarding — no lighthouse change.
  Opt-out per call with `navig social fan-out --no-track`; the receipt still stores the real URL.
- **Engagement ledger — the measure half (loop closed).** A second `BaseStore` ledger
  (`navig_social.social.engagement`) records per-campaign metrics (clicks / views / likes …), joined
  to the receipt ledger on the campaign slug. Ingest three ways: `navig social engagement <campaign>
  --metric clicks --value N` (CLI), `POST /api/deck/social/engagement` (HTTP, module-gated, mounted via
  the plugin hook), or `record_engagement_from_event()` (the signals adapter). View joined:
  `navig social receipts --with-engagement` adds a per-campaign engagement column / rollup line.
- **`navig social sync` — automatic view/CTR capture (no more manual entry).** Walks the publish
  receipts, asks each network's public-metrics API for the current **absolute** counts, and stores
  them in the engagement ledger so `navig social report` shows a *real* CTR. New `fetch_metrics(post_id)`
  seam on `BasePublisher` (default `None` = no read API), implemented for **twitter** (tweet
  `public_metrics` → views/likes/reposts/replies) and **dev.to** (article stats → views/likes/replies);
  networks without a read API (telegram, facebook …) are skipped, not failed. Backed by a new
  `EngagementStore.upsert_metric()` that *replaces* an absolute metric per (campaign, network, post, source)
  — so re-running `sync` refreshes the latest numbers instead of double-counting (the SUM rollups stay
  correct for additive click *events*). `--campaign <slug>` to scope, `--json` for scripts.

## 0.1.0 — 2026-07-07

### Added
- **Extracted from `navig-media`** — the social publishing + Studio surface is now its
  own standalone plugin. Commands `navig social` and `navig facebook` (`fb`) are
  unchanged; the Studio deck routes (`/api/deck/studio/*`) and the background
  `ScheduledPostService` move here, gated on the new free `social` module.
- **Cybesis fan-out** — `navig social fan-out --file brief.json --to
  x,facebook,devto,telegram [--campaign <slug>] [--dry-run]`: publish one brief to many
  networks in a single call, with per-platform adaptation (X ≤280, Dev.to article +
  clean canonical, Facebook/Telegram full text) and UTM tagging
  (`utm_source/medium/campaign`). `--dry-run` previews the exact per-platform payloads.
- **`TelegramPublisher`** — a standalone bot-token publisher (`api.telegram.org`
  `sendMessage`), token from vault `telegram`, chat id from
  `adapters.social.telegram.chat_id`. Distinct from the gateway's live TelegramChannel.

### Notes
- Publishing framework (`social/`), OAuth connect, dispatcher, and the 9 built-in
  publishers are byte-for-byte the `navig-media` code, re-homed under `navig_social`.
