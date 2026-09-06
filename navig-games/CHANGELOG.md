# Changelog — navig-games

## 0.9.9 — 2026-09-04

### Fixed
- **Requires `navig>=3.25.0`.** 0.9.8 declared `navig>=3.24.0`, and the published navig 3.24.0
  does not contain modules this package imports — `core/pyproject.toml` had said 3.24.0 for 541
  commits, so the source and the wheel answering to that number were different code. Installing
  with the navig it asked for produced `ModuleNotFoundError`: immediately if the import was at
  module scope, otherwise the moment the feature ran. navig 3.25.0 contains them, and
  `scripts/check_plugin_core_floor.py` now checks every declared floor against the file list of
  the wheel it actually selects.

## 0.9.8 — 2026-07-20

### Fixed
- **Claim/login no longer orphan a headful browser (or wedge the claim lock forever).**
  The background `claim` awaited its `navig games claim` subprocess with **no timeout** —
  a wedged Epic browser would hang the task forever, so the `finally` never cleared
  `_claim_lock["running"]` and every future claim returned 409 permanently, with the
  browser left running. It now runs under a `_CLAIM_TIMEOUT_S` (15 min) `wait_for` and
  **kills the child** on timeout. The `login` capture route already had a `wait_for` but
  `wait_for` only cancels the coroutine, not the child — it now `proc.kill()`s the headful
  sign-in browser on timeout instead of orphaning it.

## 0.9.7 — 2026-07-17

The scheduled counterpart to 0.9.6's "ending soon" view: a one-time reminder so a
giveaway you meant to grab doesn't expire unnoticed.

### Added
- **"Grab it before it's gone" reminder.** The daily deals run now sends one
  high-priority notification — `⏰ N free games ending soon` — for giveaways you
  were told about earlier, haven't grabbed, and that now end within
  `ENDING_SOON_DAYS` (2). Batched (soonest first, each with store · when · price ·
  link), not one ping per game.
- **Alert-once.** Each giveaway is reminded once, not every day (new
  `engine/expiry_state.py`, next to the ledger/deals state). It re-arms if the
  giveaway is re-listed, and prunes anything that leaves the feed.

### Changed
- The "ending soon" threshold is now one shared constant
  (`engine.models.ENDING_SOON_DAYS`) driving the `check` red countdown, the ⏰
  line, and this reminder — so the CLI view and the notification can't drift.

### Notes
- Excludes brand-new giveaways (they get the existing "free to keep" alert) and
  anything already grabbed/claimed. Steam free-to-keep carries no end date, so the
  reminder naturally targets the cross-store giveaways that do. Best-effort — a
  reminder never breaks the deals run.

## 0.9.6 — 2026-07-17

Grab-before-it's-gone. A dozen giveaways can be free at once — but nothing told
you which ones expire first. Now the free feed leads with the most urgent.

### Added
- **`navig games check` counts down.** The "Ends" column is now a colour-graded
  countdown — `today` / `tomorrow` / `in 2d` (red) · `in 5d` (yellow) · `in 20d`
  (dim) · `—` for open-ended — and the list is sorted **soonest-first**.
- **An ⏰ warning line** when anything you haven't grabbed yet ends within two
  days: `⏰ 2 ending within 2 days — grab them now`.
- **`ends_in_days`** is now on every free-game row in the API (`/api/deck/games/free`)
  and `--json`, so the deck/OS Free tab can lead with urgency too. Open-ended or
  unknown end dates are `null` (never a fake number).

### Notes
- Countdown is whole-day, computed from `ends_at`. Day granularity is deliberate —
  the community giveaway feeds don't carry a reliable timezone, so hour precision
  would be false confidence. Helper: `engine.models.days_until`.

## 0.9.5 — 2026-07-17

The "sign-in expired" signal now clears itself the moment you re-authorize —
no waiting for the next scheduled claim. Before, after you ran `navig games
login epic` (the exact fix the alert told you to do), status still nagged
"couldn't sign in — run navig games login epic" until a claim happened.

### Fixed
- **Stale "sign-in expired" after you already fixed it.** `epic_session_expired`
  now resolves as soon as a session is captured *after* the failed run — every
  sign-in path stamps a fresh `captured_at`, so the signal compares that against
  the failed run's time and flips to healthy immediately. No new state, no login-
  path coupling; when it can't confirm a newer sign-in it stays expired (fail
  toward surfacing the problem).
- `navig games status` shows the resolved state honestly: `○ couldn't sign in ·
  Xh ago · re-authorized, claim due next run` (the stale "run login epic" nudge
  is dropped) instead of repeating the alert.
- The deck/OS `epic_session_expired` flag (and the tile's amber "sign-in expired"
  chip) clears on re-login too — same shared `epic.epic_session_expired()`.

## 0.9.4 — 2026-07-16

Carry the "sign-in expired" truth to the status surfaces — cheaply. The live
check (`doctor --live`, 0.9.2) opens a browser, so it can't run on every status
poll. But the scheduled claim already records whether it could sign in, so we can
show the truth for free.

### Added
- **`navig games status` now has a "Last claim" row** — the outcome of your most
  recent real auto-claim (no browser): `● 1 claimed · 3h ago`, or, when the
  session died, `⚠ couldn't sign in · 6h ago — run navig games login epic`. This
  makes a failing daily claim visible without `--check`.
- **`epic_session_expired`** in the deck/OS status payload (`/api/deck/games/status`)
  — true when the last auto-claim couldn't sign in, so the Games tile can prompt a
  re-login instead of showing a stale green "signed in" (vault presence alone can't
  tell a live session from an expired one). Derived from `last_run`, no browser probe.
- Shared one-liner `last_run.login_needs_signin()` so the CLI and the route agree
  on the definition.

### Notes
- Reflects the last run, not this instant — honest and timestamped, and it
  self-corrects on the next successful claim. The on-demand live probe
  (`doctor --live` / `status --check`) remains the way to check *right now*.

## 0.9.3 — 2026-07-16

Proactive, non-annoying Epic sign-in alert. When your saved session expires, the
scheduled claim can't sign in — and it used to fire a "⚠️ Needs you" notification
*per game, on every daily run*. Two free games, session dead for a week = 14
identical pings. That's the drip that trains you to mute the one alert that matters.

### Fixed
- **The daily "not signed in" drip.** A dead/absent session (every game comes back
  "needs you: not signed in") now collapses into **one** high-signal alert —
  *"🔑 Epic sign-in expired — N free games waiting, run `navig games login epic`"* —
  instead of one ping per game. It's **alert-once**: it won't repeat on the next
  daily run for the same batch, re-alerts when a *new* week's freebies appear, and
  re-arms once you sign back in (so a future expiry alerts again). New
  `engine/login_state.py` holds the alert-once state next to the ledger/deals state.
- Genuine **per-game** manuals (region lock, checkout captcha, add-on needs the base
  game) are unaffected — those only happen once you're signed in, so they keep their
  individual alerts.

### Notes
- A `--dry-run` is a check you're watching, not the unattended claim, so it never
  fires the alert. The alert is best-effort — a notify hiccup never blocks a claim.

## 0.9.2 — 2026-07-16

Tell the truth about the Epic login. A saved session can quietly **expire**, and
until now every status/health surface reported vault *presence* as good-to-go — so
the one row you'd check before an unattended claim stayed green over a dead session.

### Added
- **`navig games doctor --live`** — opens the Epic profile and reads the real
  `egs-navigation` login state, so an expired session shows as a red
  `session EXPIRED — re-run navig games login epic` instead of a cheerful tick.
  Without `--live`, the "Epic session" row honestly says *session saved · pass
  --live to verify it still works* (never claims the session is live).
- **`navig games status --check`** — the same live probe inline in `status`
  (`● live · <account>` / `✗ session expired`). Default `status` shows
  `● session saved (<account>) · --check to verify`, no longer overclaiming.
- **`epic.probe_live_login()`** — one shared authoritative live-session check
  (launch profile → load store → read login state), reused by both surfaces.

### Why
Vault-presence can't tell a working session from an expired one; only the live page
can. A green light over an unverified thing is worse than a red one — it tells you
not to look. This makes the check opt-in (it opens a browser) but honest by default.

## 0.9.1 — 2026-07-12

Durability sweep — the stores that back this plugin all had the same shape (load a
snapshot, save the whole file) and three of them could lose data across processes.
This is the follow-through on the ledger bug found in 0.9.0.

### Fixed
- **Settings could be erased by the daemon.** `settings.set` (the deals watchlist,
  country, region…) is written from *two* places — the deck route inside the
  long-lived daemon, and the CLI in its own process. The daemon caches the global
  config for its whole life, so saving its snapshot back overwrote everything any
  CLI run had written since boot. It now goes through core's new
  `ConfigManager.set_global` (refresh → deep-set → save).
- **`deals.json` lost alert-once marks.** A deals run loads the state at the start and
  saves ~30s later; the scheduled job and a manual `navig games deals notify` can
  overlap, and the second save dropped the first's marks — **re-announcing deals you
  had already been told about**. `save()` now merges onto the current file, keeping
  the strongest discount either run announced (alert-once is monotonic) minus what was
  explicitly cleared.
- **`last_run.json` was not written atomically** (the only store that wasn't). The
  claim runs in a subprocess while the daemon's `/games/status` route reads that file,
  so a half-written file could be read — the Status tab would silently report "no last
  claim". A crash mid-write truncated it permanently. Now tmp + atomic replace, like
  the ledger and deals state.

## 0.9.0 — 2026-07-12

**"Got it"** — the terminal state for every game we can't claim for you.

Twelve of the thirteen rows in the Free tab (itch · IndieGala · Stove · GOG ·
Steam free-to-keep) could never *settle*: nothing we do and nothing you do would
take them off the list. They resurfaced every day, and once the daily deals job had
seen them they sat in History as an amber **"needs you"** forever — even for a game
you grabbed last week.

### Added
- **`grabbed`** ledger status + `navig games grab <key> [--undo]` — mark a game as
  got-it-yourself. It settles: gone from the free list, green *"got it"* in History.
- **`POST /api/deck/games/grab`** `{game, grabbed}` — the same rule over HTTP.
- OS Free tab: a **"Got it"** button on every row we can't claim, and the resulting
  *"✓ got it"* badge is a one-click **undo**.
- On an *Epic* game, marking doubles as **"skip this one"** — the scheduled claim
  honours the ledger, while a deliberate `claim --game <key>` still overrides it
  (a forced per-game claim never consults the ledger).

### Fixed
- **Ledger lost updates (data loss).** `Ledger` loaded once at construction and saved
  the *whole* file, but the claim runs in a **subprocess** while the daemon can be
  writing — so the later writer silently erased everything the other had recorded
  (reproduced: a *claimed* Epic game vanished when a grab landed after it). Writes
  now re-read from disk immediately before mutating. Regression-tested.
- **Un-marking can no longer rewrite real history**: a game we actually claimed, or
  that the store reported as owned, refuses the undo (409) — that's an audit record,
  not a user note.
- `navig games check` had not kept up with the three-source feed: the table showed no
  **store** (an auto-claimable Epic row looked identical to an itch giveaway), no
  ledger **status**, and no **key** — which made the new `grab <key>` unusable, since
  the key was never printed. All three are columns now, and the footer names both
  paths (auto-claim vs grab).
- Stale copy: `--store` help and the runner's unsupported-store error still listed
  only `all | epic | steam`, omitting `giveaways` (added in 0.8.0).

### Internal
- Sourcing extracted to `runner._source()` — the single read path shared by `check`
  and `mark_grabbed`, so the three feeds can't drift apart.

## 0.8.1 — 2026-07-12

Every giveaway now says **what it actually takes** to get it.

### Added
- `FreeGame.instructions` (empty for Epic/Steam) — the cross-store feed's real,
  human-readable steps, whitespace-collapsed onto one line (e.g. *"1. Visit the
  giveaway page. 2. Log into your free IndieGala account. 3. Click 'Add to Your
  Library'."*). Free-tab giveaway rows render it under the price (clamped to two
  lines, full text on hover), so "grab it on itch.io" no longer overstates a
  one-click grab.

### Notes
- Deliberately **not** a heuristic "needs signup / indirect claim" chip: every
  current entry is a plain "log into the store, add to library", so such a
  classifier would be unvalidatable guesswork. Showing the real steps conveys the
  effort honestly — and will reveal a raffle-style giveaway by itself if one appears.

## 0.8.0 — 2026-07-12

**Cross-store giveaways** — free games beyond Epic and Steam (itch.io, GOG,
IndieGala, Ubisoft, Stove …).

### Added
- **`engine/sources/giveaways.py`** — the free games that *aren't* on Epic or Steam.
  Sourced from **GamerPower's public, keyless giveaway API** (purpose-built and
  structured) rather than scraping a community relay: Reddit's JSON API now 403s
  unauthenticated, and the Telegram mirrors are IFTTT reposts with shortened links.
  Entries for **stores we source natively (Epic, Steam) are dropped**, so the Free
  tab can never show the same game twice — this feed only fills the gap. Non-PC,
  inactive and non-game (DLC/loot/beta) entries are filtered out; titles are cleaned
  (`"Madness Inside (itch.io) Giveaway"` → `"Madness Inside"`); cached 5 min; never
  raises (one feed failing can't break the others).
- `runner.check(store=…)` gained **`giveaways`** (and still merges everything under
  the default `all`). Free-tab rows carry their store glyph and a **"grab it on
  itch.io / IndieGala / …"** hint — nothing outside Epic is auto-claimable, so these
  never enter the claim path.
- The daily deals run now announces **new giveaways from both** Steam free-to-keep
  *and* the cross-store feed (first-seen dedupe via the ledger).

### Fixed
- Two `check()` tests were reaching the live network once a third source was added —
  isolated them.

## 0.7.0 — 2026-07-12

**Steam free-to-keep giveaways** — navig-games finds free games on a second store.

### Added
- **`engine/sources/steam.py:free_to_keep()`** — Steam games that are free to keep
  right now, sourced from Steam's **own keyless store search**
  (`maxprice=free&specials=1&json=1`). This is the authoritative upstream that the
  community "free Steam games" Telegram bots/channels merely relay, so we source it
  directly instead of scraping a mirror. The search payload carries **no appid** —
  it's recovered from the capsule-image path — and every candidate is then
  **re-verified through `appdetails`**: a giveaway is a *paid* game at 100% off
  (`is_free=false`, `final==0`, `discount==100`), so a permanently **free-to-play**
  title can never be announced as a giveaway. Cached (5 min), names resolved
  concurrently, and never raises (a Steam hiccup can't break the Epic feed).
- **Free tab is multi-store**: Epic + Steam, each row store-glyphed. Epic keeps its
  one-tap Claim; Steam rows say **"grab it on Steam"** with a store link (Steam has
  no headless claim, so Steam games never enter the claim path — the FREE-ONLY claim
  engine stays Epic-only).
- **Giveaway alerts**: the daily deals run now also announces new Steam free-to-keep
  games (`notify_free_to_keep`), deduped first-seen via the ledger so each giveaway
  is announced once.
- `runner.check(store="all"|"epic"|"steam")` (default **all**); `navig games check
  --store` follows.

### Removed
- Dead + stale `SUPPORTED_STORES = ("epic",)` constant.

## 0.6.2 — 2026-07-12

Deals watchlist hardening.

### Changed
- **`watchlist_named` resolves names concurrently** (bounded pool, mirrors
  `check_deals`) instead of one sequential `appdetails` call per game — and it now
  resolves the **whole** watchlist. Removed the silent `[:50]` cap that hid extra
  watches with no indication. Live: 5 games ~0.88s vs ~5× sequentially.

### Fixed
- **Concurrent watchlist edits could lose an update** — `add_watch`/`remove_watch`
  did an unguarded read-modify-write of `settings["steam_watch"]`, and the deck
  route runs them in worker threads. Serialized both with a lock.

## 0.6.1 — 2026-07-12

Manage the Steam deals watchlist from the OS app (the last Deals CLI-parity gap).

### Added
- **Deals tab → "Watching" section**: add a game not on your Steam wishlist by
  pasting a **Steam appid or store URL**, see your manual watchlist with resolved
  names, and **unwatch** per row. Backed by `POST /api/deck/games/deals/watch`
  `{appid, watch}` (add/remove) and `GET /api/deck/games/deals/watchlist` (names via
  appdetails).

### Changed
- Centralized the manual-watchlist logic into `engine/sources/steam.py`
  (`parse_appid` / `get_watchlist` / `add_watch` / `remove_watch` / `watchlist_named`);
  the `navig games deals watch|unwatch|list` CLI now shares it (removed the
  duplicated settings-mutation + the `_parse_appid` copy in `commands/games.py`).

## 0.6.0 — 2026-07-12

Per-game claim + "owned" badges on the Free tab.

### Added
- **Free tab is now per-game aware.** `/games/free` tags each current freebie with
  its ledger `claim_status` (`runner.check` joins the ledger), so each row shows
  **"✓ owned" / "✓ claimed"** for games you already have, or a per-game **"Claim"**
  button for the rest. The header's bulk button now claims only the *unclaimed*
  count ("Claim N"), and hides when everything is owned.
- **Per-game claim** end to end: `run_claim(only=<key>)` filters to one game;
  `navig games claim --game <key>`; `POST /api/deck/games/claim {game}` (the claim
  job gains a `game` field). After a claim the Free tab refreshes so badges update.

## 0.5.9 — 2026-07-12

Make unattended claiming observable — a "Last claim" health line on Status.

### Added
- **`engine/last_run.py`** — a tiny rollup store (`<config_dir>/games/last_run.json`)
  written after each *real* claim run (dry-runs are ignored, so it always reflects
  actual scheduled/confirmed claims). Captures `finished_at`, `checked`,
  `attempted`, `claimed`, `needs_manual`, and the sign-in `login` mechanism. Best-
  effort — never blocks a claim.
- **Status tab → "Last claim"** row: an at-a-glance health line — e.g. "1 claimed ·
  2h ago" (green), "3 need you" (amber), or "nothing new · 5h ago" — with
  "via saved session · N checked" on hover. Confirms unattended auto-claim is
  actually running without digging through History. `status` now returns `last_run`.

## 0.5.8 — 2026-07-12

Turn automations on/off from the OS app — the core "autonomous claiming" promise
was CLI-only.

### Added
- **`POST /api/deck/games/schedule`** `{job: claim|deals|unify, enabled}` — the
  `navig games schedule enable/disable` capability over the deck API. Prefers the
  **live `CronService`** (`get_live_service()` → add/enable/remove by name) so a
  toggle takes effect **immediately, no daemon restart** — and persists to the same
  jobs file schedule.py reads (compatible shape), so `status` stays consistent.
  Falls back to the file-write path when no live service is present (e.g. cron off).
- **navig-os Status tab**: the read-only Automations chips are now **toggle
  buttons** — Auto-claim (daily), Deal alerts (daily), Auto-unify to Steam (weekly).
  Enabling Auto-claim confirms first (it runs unattended, FREE-ONLY). Optimistic
  state + sonner toast.

## 0.5.7 — 2026-07-11

Make the claim/restore visible — a real audit surface + sign-in telemetry.

### Added
- **History tab now shows the reason** behind each entry — the ledger `note`
  ("already in your library", "not signed in — run `navig games login epic`") was
  recorded but never surfaced. Now shown as a dim, hover-full line under each row.
- **Sign-in mechanism telemetry**: a claim reports **how** it authenticated —
  `run_epic_claims` returns `(results, login)` (`session_restored` | `filled` |
  `logged_in` | `cookies` | `needs_manual` | `error`); `run_claim` exposes it as
  `login`, the deck claim route passes it through, and the Free-tab claim toast
  confirms **"signed in from your saved session"** when a claim used the restored
  vault session — making last round's session-first fix visible.

### Fixed
- History status chips: `failed` now renders a red **error** chip (was an
  unstyled raw label); `needs_manual` / `skipped_priced` promoted to **warning**
  (amber) so attention-worthy outcomes stand out from neutral ones.

## 0.5.6 — 2026-07-11

Fixes the Epic sign-in signal and adds one-click sign-in from the OS app.

### Fixed
- **`epic_signed_in` was always false even with a valid session.** Root cause:
  `get_session(domain)` builds a vault label that includes the account
  (`web-session/epicgames.com/<user>`), so a username-less lookup never matched a
  session saved *with* an account name. Status now detects the session via
  `list_sessions()` (`engine/claim/epic.py:epic_session_present`), so the Claim
  button correctly appears when you're signed in — and `status` now also returns
  the Epic **account name** (`epic_account`).

### Added
- **`POST /api/deck/games/login/capture`** + a Free-tab **"Sign in to Epic"**
  button: one click captures an already signed-in browser profile into the vault
  (button then flips to "Claim"), or opens the Epic login page to sign in and
  retry. Backed by a non-interactive `navig games login epic --capture-only`
  (spawned as a subprocess, off the daemon loop).
- **Capture-on-claim**: a claim that confirms signed-in now saves the session to
  the vault (belt-and-suspenders; mirrors `login epic`) so a browser-only sign-in
  no longer leaves the vault — and `status` — out of sync.
- Status tab shows **"signed in as &lt;account&gt;"**.

## 0.5.5 — 2026-07-11

Claiming becomes actionable from the OS app — one-tap "Claim" + a History tab.

### Added
- **`POST /api/deck/games/claim`** — claims this week's free games in the
  background (FREE-ONLY). Spawns `navig games claim` as a subprocess (the same
  isolation the scheduler uses — no event-loop conflict with the daemon's own
  browser). One claim at a time; a second request while one runs → **409**.
  Optional body `{dry_run?}`. Returns immediately.
- **`GET /api/deck/games/claim/status`** — the claim job's live status (the UI's
  poll target): `{running, started_at, finished_at, dry_run, ok, summary}`.
- **`GET /api/deck/games/history`** — the claim ledger (what's been claimed /
  attempted), newest first.
- **navig-os Games app**: a **"Claim N"** button on the Free tab (shown when a
  game is free **and** Epic is signed in — otherwise it nudges to
  `navig games login epic`) → confirm → POST → polls status → sonner toast with
  the result. A new **History** tab renders the ledger with status chips.

### Notes
- FREE-ONLY is unchanged and enforced in the engine: a real claim only ever
  completes a $0 checkout; a priced title is always refused.

## 0.5.4 — 2026-07-11

The Games app becomes actionable — "Add to Steam" from the Library tab.

### Added
- **`POST /api/deck/games/unify`** — adds the user's non-Steam games to Steam
  (writes shortcuts.vdf + best-effort GOG-local cover art). Owner-safe: loopback
  deck plane, gated on the `games` module. Backs up, dedupes, and surfaces
  "Steam is running" as **409** so the UI can prompt to quit Steam. Optional body
  `{store?, dry_run?, force?}`.
- **navig-os Games app**: an **"Add N to Steam"** button on the Library tab
  (shown when non-Steam launchable games exist) → confirm → POST → sonner toast
  with the result (added / already-present / Steam-running). (apps/os)
- `GET /games/deals?fresh=1` bypasses the deals cache (for a future refresh
  control / scripts).

## 0.5.3 — 2026-07-11

Instant deal re-opens.

### Improved
- **`check_deals` now caches results for 180s** (in-memory). The OS Games "Deals"
  tab re-fetches on every tab switch → a daemon route call; the cache makes those
  re-opens **instant** instead of a ~6s Steam round-trip, and cuts store-API load.
  Keyed by (cc, threshold, wishlist, extra); `use_cache=False` / `clear_deals_cache()`
  force a fresh fetch. The daily **notify** path always fetches fresh (no stale
  alerts). In-memory = lives in the daemon; the one-shot CLI is unaffected.

## 0.5.2 — 2026-07-11

Deals speed — found while live-rendering the navig-os Games app.

### Fixed
- **`deals` was ~30s for a large wishlist** (68 items fetched sequentially with a
  0.15s pace) — far too slow for a live UI (the OS Games "Deals" tab just spun). Now
  fetches per-app prices **concurrently** (bounded 10-worker pool): ~30s → ~6s, still
  rate-limit-safe. Benefits both the CLI and the OS app. Extracted `_deal_from()` for
  the per-app detection; dropped the `pace` param.
- navig-os Games app: the Deals tab now shows a "Checking Steam prices…" hint while
  it loads (apps/os).

## 0.5.1 — 2026-07-11

Desktop app surface (navig-os).

### Added / changed
- The games module now declares an `os-tile:games` surface + `app_category="Life"`
  + `scope="brain"`, so it renders as a **desktop app in navig-os** (backed by the
  `/api/deck/games/*` API from 0.5.0). Dropped the unused `deck-section:games`
  surface (the app lives in navig-os, not the deck).
- Frontend lives in `apps/os/apps/webui` (a `GamesApp` with Free / Deals / Library
  / Status tabs) — shipped in the same change.

## 0.5.0 — 2026-07-11

Deck / OS / remote HTTP surface.

### Added
- **Gateway API** — the games engine is now reachable over HTTP at
  `/api/deck/games/*`, so the deck UI, the OS super-app, remote access (lighthouse)
  and scripts can consume the same data as the CLI. Routes:
  - `GET /api/deck/games/status` — plugin + subsystem status
  - `GET /api/deck/games/free` — current & upcoming free games (Epic)
  - `GET /api/deck/games/deals?threshold=` — Steam wishlist deals + free-to-keep
  - `GET /api/deck/games/library` — installed games across launchers
  Mounted via core's `gateway:register_routes` hook (mirrors navig-social), gated on
  the `games` module (returns `403 module_disabled` when off, live). Blocking engine
  calls run in a worker thread so the gateway stays responsive. This completes the
  plugin's surface: **CLI + agent tools + gateway API**.

### Notes
- GOG free-game claiming was evaluated and deferred: the giveaway endpoint 404s when
  inactive and the catalog `price=0` returns demos, so there's nothing to source
  cleanly/validatably right now. Revisit when a giveaway is live.

## 0.4.1 — 2026-07-10

Hardening & polish pass across the whole plugin.

### Fixed
- **SteamGridDB lookups were broken for most games** — the game title was
  interpolated raw into the request URL, so any name with a space or colon
  ("SYNTHETIK: Legion Rising") produced a malformed request. Now URL-encoded.
- **`deals` fetched your wishlist twice** per run (once directly, once inside
  `watched_appids`) and resolved the SteamID 3×. Now fetched exactly once.

### Added
- **Scheduled auto-unify** — `navig games schedule enable --job unify` keeps your
  Steam library in sync with newly-installed Epic/GOG/Amazon games on a cadence
  (reuses the generalized scheduler; `schedule status` now lists all three jobs).

### Improved
- **`navig games doctor`** now health-checks *every* subsystem — Epic sourcing,
  browser stack, vault, Steam detection, cross-launcher library scan, cover-art
  grid folder, and Steam-deals reachability — instead of just the Epic trio.
- **`navig games status`** now reports installed-game counts, Steam detection +
  non-Steam shortcut count, SteamGridDB-key state, deal-watchlist size, and all
  three schedules.

## 0.4.0 — 2026-07-10

Steam deals watcher — wishlist price-drop + free-to-keep alerts.

### Added
- **`navig games deals`** — shows current price drops and free-to-keep giveaways
  on your Steam **wishlist** (auto-detected via SteamID64 from the installed Steam)
  plus a manual watchlist. Public Steam APIs (`IWishlistService` + store
  `appdetails`) — **no key, no login**.
- **`deals watch/unwatch/list`** manage a manual appid watchlist;
  **`deals notify`** fires notifications for anything new (the scheduler target),
  with alert-once state so a daily run never re-spams the same sale.
- **`navig games schedule enable --job deals`** — the scheduler now runs either the
  claim job or the deals job; `schedule status` lists both.
- Read-only agent tool **`games_deals`**; deal notifications via `navig.notify`
  (free-to-keep = high-priority per-game alert, price drops = one roll-up).

### Improved
- **Generalized `schedule.py`** (`upsert_job` / `remove_job` / `job_status`) so a
  second scheduled job isn't a copy-paste of the first; the Epic `enable/disable/
  status` wrappers stay backward-compatible.

### Notes
- The old public wishlist endpoint is dead; `IWishlistService/GetWishlist/v1` works
  keyless. Live-verified against a 68-item wishlist. Concept: isthereanydeal-style
  watchers — native reimpl.

## 0.3.0 — 2026-07-10

Cover art for unified Steam shortcuts (completes `unify`).

### Added
- **Steam grid cover art** — `navig games unify` now fetches art automatically and
  writes the Steam grid files (`<appid>p/_hero/_logo/_icon`) so unified games look
  native instead of showing a blank tile. `navig games art [game|--all]` fetches or
  refreshes art on demand, with `--portrait/--hero/--logo/--image` for manual files.
- **Keyless art** from the launcher's own local data (GOG → hero + logo + icon, no
  API key). Optional **SteamGridDB** for full portrait grids: `navig games art
  --set-key <key>` (or `--steamgriddb-key`).
- `unify --no-art` to skip; the shortcut's `icon` field is set when an icon is found.

### Fixed / improved
- GOG scanner now copies the DB to a **unique** temp file (was a fixed name — a
  concurrency hazard) via a shared read helper (removes duplicated DB-access code).
- Extracted `shortcut_appid_for(game)` as the single source of truth for the appid
  shared by the shortcut entry and its grid-art filenames (was computed in two
  places, risking art/shortcut id drift).

### Notes
- GOG has no local vertical cover, so the main portrait tile needs SteamGridDB or a
  manual `--portrait`. Concept: SteamGridDB / BoilR — native reimpl.

## 0.2.0 — 2026-07-10

Steam suite (library slice) + Epic hardening.

### Added
- **Library discovery** — `navig games library [--store …] [--json]`: scans
  installed games across **Steam, Epic, GOG and Amazon** from on-disk manifests
  (registry / VDF / SQLite), no launcher login. Read-only agent tool `games_library`.
- **Unify launchers into Steam** — `navig games unify [--dry-run] [--force]`: adds
  your Epic/GOG/Amazon games to the Steam library by writing `shortcuts.vdf`
  (own binary-VDF codec). Non-destructive: backs up the file, dedupes by
  executable, and refuses while Steam is running. `navig games steam shortcuts`
  lists what was added.
- **Steam Guard authenticator** — `navig games steam auth (--shared-secret | --mafile)`
  stores the secret in the vault; `navig games steam code` / `steam accounts`
  print the current 5-char 2FA code (canonical Steam TOTP, verified against the
  reference algorithm).

### Fixed
- `schedule._next_id` return type; removed two dead `table.add_column` no-ops;
  filter Steamworks redistributables/runtimes out of the Steam library list.

### Notes
- Non-Steam shortcut unification (shortcuts.vdf + manifest scan) and Steam Guard
  TOTP are implemented natively.

## 0.1.0 — 2026-07-10

Initial release. Epic Games Store, end-to-end.

### Added
- `navig games check [--upcoming] [--json]` — current & upcoming free games from
  Epic's public `freeGamesPromotions` API (no scraping).
- `navig games claim [--dry-run] [--all] [--strict] [--yes] [--json]` — **FREE-ONLY**
  auto-claim driving a persistent, isolated browser over core's CDP stack with
  session-first vault login. Three independent $0 gates; a priced title is always
  refused. `--dry-run` verifies free and stops before the order.
- `navig games login epic` — one-time sign-in; session saved to the vault so
  claims run unattended thereafter.
- `navig games schedule enable|disable|status` — recurring auto-claim via core
  `CronService`.
- `navig games history | status | doctor`.
- Agent tools `games_check` (read-only) and `games_claim` (owner-only, hidden
  until an Epic session exists).
- Idempotent claimed-games ledger; notify fan-out ("Claimed X") via
  `navig.notify.dispatch`.

### Notes
- The claim and sourcing flows are implemented natively — first-party code, no
  third-party engine imported.
- GOG, Amazon Prime Gaming and the Steam suite are planned follow-ups.
