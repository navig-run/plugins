# Changelog — navig-social

## Unreleased

### Added
- **`EngagementStore.post_metrics(campaign, network=None)`** — per-post totals inside one
  campaign (`{"e07": {"views": 1200}}`), the read a series needs to compare its episodes.
  Used by `navig pipeline episode stats`, which records TikTok snapshots here (campaign =
  series, post = episode) instead of keeping a second store; they also show in
  `navig social report`. Version → 0.2.2: `check_plugin_version_drift.py` found 13 source
  files changed since 0.2.1 was published, so none of this section could reach PyPI users.
- **G6 contract ingest — idempotent submission from a foreign producer.**
  `POST /api/deck/studio/posts` with an `external` block (`content_id`, `project_id`,
  `campaign_id`, `content_hash`, `approval_policy`) upserts on `content_id`: first submit
  `created` (201), a retried submit `replay` (200, untouched), a changed hash `updated`
  (version+1, approval invalidated, required-policy posts re-enter review), and `409
  idempotency_conflict` once terminal — always answering the SOCIAL-CONTRACT §2 envelope
  (validation warnings, approval, per-destination state, manual-handoff list). The §3 hash
  rides in as `content.content_hash`; phase-1 captions (links/sign-up) and targets outside
  the contract's destinations refuse with 422. `GET /studio/posts?campaign=&content_id=`
  filter the list; `GET /api/deck/social/receipts` (`?campaign=&content_id=&network=`)
  reads the ledger over HTTP.
- **Publish route gates.** `POST …/publish` accepts `Idempotency-Key` as header or body,
  answers `410` for expired items and `409 approval_required` when a required approval is
  not bound to the current hash (the scheduler keeps its parked `awaiting_approval`);
  PATCH refuses content edits on terminal posts (`409 immutable`) and demands
  `contract`/`content_hash` in-payload for external posts.
- **Content approval + versioning (Stage B of SCHEMA's SOCIAL-CONTRACT, spec in
  wave1/NAVIG-DEPENDENCIES.md G1–G5):**
  - `social/content_hash.py` — `contract_hash` implements the cross-repo §3 algorithm
    **byte-pinned** by `tests/social/fixtures/contract_hash.json` (computed by SCHEMA's
    `navig_contract.content_hash`; both repos must never drift), plus `of_post` for
    Studio content. Schedule/timezone/policies are excluded from the hash — rescheduling
    never voids an approval; text/media/destinations changes do.
  - Store v2 (`scheduled_posts`): `content_id/content_hash/version/approval_state/
    approved_hash/approved_at/idempotency_key` columns + `post_approvals` +
    `post_versions` tables (additive migration; existing posts default `approval_state=
    none` → behaviour unchanged). New: `set_content` (rehash → version bump → approval
    invalidation), `approve`/`reject` (hash-bound decisions), `get_by_content_id`,
    `approval_ok`.
  - Scheduler gate: `run_post` skips `approval_policy="required"` posts unless
    `approved_hash == content_hash` (stays `scheduled`, `last_error="awaiting approval"`);
    `auto` (default, YouTube semantics) bypasses untouched. Idempotency: replayed key on a
    published post returns the stored result — never a second publish.
  - Routes: `POST /studio/posts/{id}/approve|reject` (409 on stale hash), PATCH of
    content/targets now rehashes + invalidates, publish accepts `Idempotency-Key`.
  - CLI: **`navig social approve|reject <post-id>`** and **`navig social track
    <content-id>`** (the §2 envelope: post + approval + per-network receipts; exit
    0 found / 1 not found).
  - Receipts ledger v2: `content_id/content_version/source/idempotency_key` columns +
    unique partial index; `record()` gains those kwargs (duplicate key = already
    recorded, returns 0); `list(content_id=…)`. **`upload` now writes a receipt**
    (source `upload`) and dedupes via `--content-id` (`--force` overrides); `fan-out`
    records identity and skips networks already ok for the content_id (prior receipt
    returned, nothing re-published).
  - `tests/conftest.py` pins THIS tree's core/plugin/sdk on `sys.path` (worktree truth
    over the editable install). 22 new tests across content_hash/versioning/approval/
    idempotency/track.
- **Several accounts per network.** Connecting a second YouTube channel, Facebook Page,
  Instagram, LinkedIn, Threads or Pinterest account now adds it beside the first. It used to
  overwrite the first account's token. The first account keeps its old slot, so existing
  installs read unchanged. Studio's composer shows one chip per account; a post target
  `@<id>` publishes as that account. `navig social disconnect|refresh --account <id>`
  act on one account. Removing the first account promotes the next, settings included.
  Accounts live in `adapters.social.<network>.accounts`.

### Fixed
- **A real v1 receipt ledger opens again.** `_create_schema` ran
  `CREATE INDEX (content_id)` before the version check — on an existing v1
  `publish_receipts.db` that raised `no such column`, store init died, and every
  recording soft-failed silently (observed live: the ledger was still v1 and
  unwritable). Both v2 indexes are now guarded, and the migrate DDL tolerates a
  re-run after an interrupted stamp.
- **Tests never touch the production data dir.** The plugin conftest isolates
  `NAVIG_DATA_DIR` per test (fresh receipts singleton) — with the brick fixed, an
  un-isolated test would otherwise migrate and write the live ledger.
- **An invalidated approval records why.** `set_content` now appends a
  `post_approvals` row when an edit voids a bound approval (state is the view;
  the hash comparison is the gate; the row is the human answer).
- **YouTube multi-channel uploads are channel-safe end to end.** Every connect now forces
  Google's account chooser, rejects identities without a channel, and records the returned
  channel id. `social upload --account <channel-id-or-unique-name>` refreshes that account's
  own token, verifies Google's channel id before sending any bytes, and never falls back to
  the default channel. The services catalog flags each channel missing its refresh token.
- A rejected dev.to key no longer replaces the working one it was meant to update.

- **Publishing networks connect from the app.** The daemon runs the same OAuth flows as
  `navig social connect`, but hands the sign-in URL to the app instead of opening a browser.
  Threads' paste-back step takes the pasted URL from the app. Each network is listed in the
  app's services catalog (`GET /api/deck/services`) with its state and what it needs: app
  credentials, a key, or one click. New routes: `/api/deck/studio/networks/{network}/connect`
  (+ `/reply`), `/credentials`, and `DELETE /api/deck/studio/networks/{network}`. A rejected
  dev.to key is not kept.

## 0.2.1 — 2026-09-30

### Added
- **`navig social relay`** — the console for navig-relay, the Cloudflare Worker that holds
  scheduled Telegram posts because bots cannot schedule (`ScheduleBotNotAllowedError`).
  `post` (text or file, `--at now|+2h|+3d|ISO`, `--dry-run`, `--force`), `queue`, `cancel`
  (by 8-character prefix), `drain`, `status` (flags stuck posts). Works on its own —
  configured from `--url`, `NAVIG_RELAY_URL`/`NAVIG_RELAY_TOKEN`, or the vault's `relay` entry.
- **Running the same `post` twice queues it once.** Found by the end-to-end check, not the unit
  tests: two identical `--at now` runs delivered two messages to a live channel, because the
  idempotency key hashed the resolved second. Relative times now key on the text, the channel
  and the day the command ran; exact times keep an exact key; `--force` is the deliberate repeat.
- A dropped connection is a clean error, not a traceback. urllib wraps only connection-*setup*
  failures in `URLError`; a reset or hang-up while the response is read escapes as a bare
  `ConnectionResetError`/`RemoteDisconnected` (~1 in 75 calls against a local server on Windows,
  first seen as a flaky test). The error says re-running is safe, which it is: enqueue is keyed,
  and the Worker's drain claims rows before sending.

### Changed
- **Browser-backed stats run without navig**: with navig-browser installed (`pip install
  "navig-social[browser]"`) the stats crawl drives `navig-browser …` directly, with the same
  commands and `--json` output as `navig cdp …`. `--only-public` still needs no browser.

## 0.2.0 — 2026-09-29

### Added
- **Runs on its own.** `pip install navig-social` gives a `navig-social` command with no navig
  installed: `status`, `connect`, `refresh`, `disconnect`, `fan-out`, `receipts`, `engagement`,
  `sync`, `report`, `stats`, `presence`, `upload`, and the Facebook Page tools as
  `navig-social facebook …`. Inside navig they stay `navig social …` / `navig facebook …`.
- App credentials for `connect` (`linkedin/client_id` …) fall back to the environment
  (`LINKEDIN_CLIENT_ID` / `NAVIG_LINKEDIN_CLIENT_ID`) after the vault: a path-labelled vault item
  can only be created with `navig vault set`, so this is how they reach `connect` on its own.
- `presence --space` accepts the space's folder path, with or without navig.

### Changed
- Vault access goes through navig-vault, non-secret settings through navig-sdk (the same
  `adapters.social.<provider>.<key>` in the same config.yaml), receipts and engagement through
  navig-sdk's store at the same paths. Depends on navig-sdk>=2.15, navig-vault>=0.7, aiohttp,
  requests, typer and rich instead of navig.
- `set_config` writes through `ConfigManager.set_global` inside navig (refresh → deep-set →
  save) instead of saving the process's cached copy of the config.
- Hints name the command that exists where you are (`navig-social …`, `navig-vault add …`,
  `NAVIG_<PROVIDER>_<KEY>=…` on their own).
- A Telegram/Discord target without navig fails its receipt with "telegram posting needs navig"
  instead of "No module named 'navig'".

### Fixed
- **`stats instagram <handle>` answered for the wrong account — silently.** When the vault's
  token linked exactly one IG Business account, the username check was skipped entirely
  (`username == h or len(candidates) == 1`), so *any* handle returned the token owner's own
  follower count as a confident `● ok`. `stats instagram this_handle_does_not_exist` returned a
  number. The handle must now match; the error names which accounts the token can actually
  answer for. This was about to be written into every roster snapshot.
- **An official API that failed ended the attempt.** A credential that existed but could not
  answer — wrong scope, or an account the token doesn't cover, such as a *personal* Instagram —
  returned the error without ever trying the browser path, even though CDP could read the public
  profile. `crawl_account` now falls through to CDP when the API can't answer, and keeps the API's
  reason in the error if the browser also fails.

### Added
- **`navig social presence crawl|trend --space <s>`** — the space-bound layer that was dropped
  when navig-presence was retired into this plugin's `stats` engine. `crawl` reads a space's
  `account-registry.json`, crawls every account, writes
  `analytics/raw/_snapshots/presence-YYYY-MM-DD.json`, and diffs against the previous snapshot;
  `trend` reports first → latest movement across all snapshots without re-crawling. `--only-public`
  makes it cron-safe. The `stats` engine itself stays space-free.
  Guarantees that matter: only `ok` results are written back to the registry (a walled run never
  erases the last number actually observed), a first sighting reports `new` rather than `+<count>`,
  a failed fetch reports `—` and never `0`, and accounts marked `"snapshot_status": "dead"` are
  skipped so a retired handle stops being re-crawled.

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
