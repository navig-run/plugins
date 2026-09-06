# Changelog — navig-download

## 0.1.2 — 2026-09-01

### Fixed
- **Declares `navig>=3.24.0`.** 0.1.1 declared no dependency on navig while importing
  it at module scope, so `pip install navig-download` installed cleanly and then raised
  `ModuleNotFoundError` on first use. Only `pip install navig[download]` ever worked.
- Ships the full Apache-2.0 licence text in the wheel.

This release also carries everything listed under *Unreleased* below.

## Unreleased — 2026-08-07 (d)

### Performance
- **`info()` is cached** (bounded, 10-minute TTL, `refresh=True` bypasses). The bot reads one post up
  to four times — card, 🎧, 📄, 🔍 — and each was its own request. Measured: 3 → 1 on the real flow.
  Keyed on the canonical URL plus the fetch options; hands out copies; never caches a failure.
  ⚠ A new `tests/conftest.py` resets the engine's process-global caches around every test — the cache
  immediately exposed 8 cases that had been silently reading each other's fake yt-dlp answers.

## Unreleased — 2026-08-07 (c)

### Changed
- **`render_card` fits the caption to Telegram's real ceiling** instead of a flat 1500 characters.
  The header and stats are measured first and the description gets the remainder, so a 1939-char
  caption (the reported post) lands whole. `card_truncates_description()` reports whether anything
  was cut — the two share `_card_parts`, so a "read the rest" affordance and the card itself cannot
  disagree about what fit. The trim is applied to the RAW text and re-measured after escaping;
  cutting the escaped string could sever an entity (`&am`) and hand Telegram broken markup.
- **`info()` now projects `artists`.** The sound's performer is not the poster — "Veins of Sand"
  is by GTMN, shared by @get.man_ — so labelling audio with the uploader is wrong on every
  reposted sound, which is most of TikTok. Empty list when unknown, never `None`.

## Unreleased — 2026-08-07 (b)

### Fixed — the CLI called every SHARED photo post a video
- The split between "video" and "photo post" used `is_photo_url`, which is **pure**: it answers
  about the string it is handed. A `vm.tiktok.com` share link — the form people actually send —
  has no path, so every shared slideshow was classified a video. `navig tt download <share link>`
  routed it to yt-dlp, which cannot read a photo post at all, and `navig tiktok info` repeated the
  check as an inline `"/photo/" in url.lower()` — a second copy of the same blind spot.
  **`is_photo_post()`** is the resolving companion and is now what callers use. `is_photo_url`
  keeps its name and its purity, with a test pinning that it does no I/O: a predicate that reads
  free must stay free.

### Changed — photo posts no longer need a browser
- `download_photo` / `download_photos` / `navig tiktok info` read the post's server-rendered item
  struct over plain HTTP (`read_post_http`) and fall back to the stealth browser only when TikTok
  did not render it (a login-gated post, where `session_host` earns its keep). Measured against a
  real photo post: `navig tiktok info <share link>` went from a Patchright launch to **0.7s**, and
  `navig tt download` to **1.1s** including fetching the image. An install with no browser engine
  now handles the ordinary case. The batch path pre-passes over HTTP and opens a controller only
  for what is left — usually none — and a batch whose browser fallback is unavailable no longer
  discards the posts HTTP already saved.

## Unreleased — 2026-08-07

### Fixed — TikTok **photo posts** were unreadable by every entry point
- yt-dlp's TikTok extractor matches only `/video/`, so a **photo post** (TikTok's slideshow
  format, `/@user/photo/<id>`) came back as a flat `ERROR: Unsupported URL` from `info`,
  `info_with_comments`, `analyse` and `fetch_file` alike. Sharing one with the bot produced a
  card reading just "🎵 TikTok link" — the no-metadata fallback, with no description at all —
  and "Couldn't extract the audio." on the 🎧 button. The share link a person actually sends
  (`vm.tiktok.com/…`) hides which kind of post it is: yt-dlp resolves it internally and only
  *then* rejects it, so nothing upstream could see the `/photo/` either.
  TikTok serves the same post id under both paths, and the `/video/` form returns the full
  description, the stats **and** the audio track. So `canonical_url` now resolves the share link
  (bounded, cached — one link is read up to five times by the card and its four buttons) and
  rewrites `/photo/` to `/video/` at the yt-dlp boundary. One place, and every entry point reads
  photo posts.
- **A slideshow is no longer downloaded as a "video".** It has no video stream — its only format
  *is* the audio track — and the download format ladder ends in a bare `best`, so the rewrite
  alone would have quietly satisfied a request for video with the audio and uploaded it via
  `sendVideo`: a file the recipient cannot play, reported as a successful download. `fetch_file`
  now raises `TikTokNoVideo` (carrying the metadata it already read) when video is asked for and
  none exists. `audio_only` is unaffected — that is exactly what a photo post *can* give.
- **New: `read_post_http` / `download_post_images`** read a post's description, stats and
  per-slide image URLs straight from the SSR'd `__UNIVERSAL_DATA_FOR_REHYDRATION__` blob — no
  browser, no Patchright launch. yt-dlp reports only the cover thumbnail, and TikTok server-renders
  the struct for the `/video/` form and not for `/photo/`, which is the same asymmetry
  `canonical_url` exists for. The projection reuses the browser tier's own helpers so the two
  readers cannot drift into two shapes.
- **The card stopped cutting descriptions silently.** It kept 500 characters and marked nothing,
  so a truncated caption was indistinguishable from a complete one. Telegram's ceiling is 4096; it
  now carries 1500 and appends `…` when it clipped. A photo post also says so, which explains why
  ⬇️ returns slides rather than a clip.

## Unreleased — 2026-08-06

### Fixed — the DOWNLOAD path was the one route that never reported a bot-wall
- `info` and `info_with_comments` both run `_raise_if_blocked`, and both are tested for it.
  `fetch_file` — the route every Telegram *button* takes — did not, so a 403 / 429 / captcha
  surfaced as a bare yt-dlp error. Three handlers in `navig.telegram.tiktok_actions` were written
  to catch `TikTokBlocked` on exactly this path and **could never fire**: the user hit TikTok's
  single most common failure and was told "couldn't download that video", with no hint that it is
  transient or that a proxy / cookies would fix it. `fetch_file` now classifies exactly as the
  metadata paths do — the fix belongs at the one shared reader, not at each of the callers.
  `TikTokUnavailable` still passes through untouched (a missing downloader is a different fix),
  and a genuine bug stays a genuine bug. Guarded by `tests/test_fetch_file_blocked.py`, including
  through the `fetch_file_async` thread hop the bot actually calls.

## Unreleased — 2026-07-12

### Fixed — a username / URL can no longer write outside the output dir (path traversal)
- `sanitize_username` was misnamed: it only stripped `@` + whitespace, then its result was
  `os.path.join`ed onto the output dir and passed to `os.makedirs` — so a handle like `../../evil`
  (or a crafted URL whose `.../<user>/video/<id>` username segment is `..`) created dirs and wrote
  files **outside** the download folder. It now reduces the name to a filesystem-safe segment
  (`[A-Za-z0-9_.@-]`, path separators → `_`, leading/trailing dots stripped, empty result refused),
  and `download_from_url` routes its URL-parsed username through it (and strips leading dots off the
  video-id). Real TikTok handles are unaffected. Guarded by `tests/test_sanitize_username_path.py`.

### Improved — comment activation stops as soon as it works (no ~15s wait on empty videos)
- The tab-activation loop now breaks the moment the comment API **fires** (activation succeeded —
  the request only goes out once the tab is active), not only when comment *items* render. A
  genuinely-empty video used to burn ~15s retrying the click waiting for items that never appear;
  it now settles in one round. Snappier for normal videos too, and the watcher is removed each run
  so a reused controller (photo batch) never stacks listeners. No change to what's fetched.

### Fixed — comment fetch works on non-English TikTok UIs
- **The 'Comments' tab is now found across languages.** The tab activation matched only the English
  word "Comments", so a user whose TikTok UI is French/Spanish/Japanese/… would get **zero comments**
  (the tab click missed). It now matches the label across ~25 major TikTok UI languages
  (`Commentaires` · `Comentarios` · `Kommentare` · `コメント` · `评论` · `Комментарии` · `التعليقات` …),
  with the language-independent `[data-e2e="comments"]` tab as a synthetic fallback for anything not
  listed. Verified the label matcher across 9 scripts + an English live regression.

### Fixed — `navig tt analyse` now weighs real comments (not just the caption)
- **The AI briefing finally includes the audience's comments.** `analyse` fetched everything via
  yt-dlp, whose signed comment endpoint is gated for TikTok videos, so the briefing was
  description-only despite its "description + best comments combined" promise. Now, when yt-dlp's
  comments are gated, `engine.analyse` recovers the top comments via the stealth browser (Tier-B,
  no redundant Tier-A re-run) before briefing — verified: `@khaby.lame` video → 6/2 159 comments
  recovered, briefing generated from them.
- **`--login` on `navig tt analyse`** + automatic session restore, so EU-gated comments load
  (extracted `engine.brief_meta` so the escalation briefs once; the `/photo/` fallback now briefs
  too instead of "briefing skipped", and both paths render through one `_render_analysis` — header ·
  stats · briefing · house-style comment table).

### Improved — `navig tt comments` just works (no `--browser`), `--json`, tabular output
- **The self-healing ladder is now the default** for `navig tt comments` — it tries yt-dlp fast
  first, then auto-escalates to the stealth browser (which reads TikTok's signed comment JSON). No
  more "served none — re-run with `--browser`" two-step; comments come back in one command. `--browser`
  is kept as a hidden no-op for backward compatibility.
- **`--json`** on `navig tt comments` emits `{comments, tier, blocked, healed, comment_count}` for
  scripts/agents (progress line suppressed so output stays machine-clean); humans still get the table.
- **Comments render as a house-style Rich table** (rank · ❤ likes · author · wrapping comment column),
  markup-injection-safe, with a `N of TOTAL · tier X · healed` summary — shared by `comments` and `post`.
- **Session-aware empty-state** — when every tier is gated, the hint now checks whether a session
  exists: "run `navig tt login`" vs "your session may have expired — try --headful / a --proxy".

### Improved — comment pagination is complete + fast (early-stop) and covers photo posts
- **Multi-page comment pagination verified + scaled.** On a 2 100-comment video the fetch now pages
  the comment list (cursor 20→200) and returns the full `max_comments` ask; the round budget scales
  with the ask instead of a fixed 8. `_SCROLL_JS` scrolls the **comment-list container** (not the
  video feed), so pages actually advance.
- **Early-stop** — the scroll loop stops as soon as enough comments are collected **or** TikTok
  signals the last page (`has_more` falsy), so a 2-comment video finishes in ~1 page instead of
  running every round (via a new `stop_when` predicate on `capture_json`).
- **`/photo/` posts get comments too** — the same headful/desktop-layout/Comments-tab path now
  returns photo-post comments (previously 0), alongside the images + description.

### Fixed — TikTok comments now actually come through (headful-offscreen browser tier)
- **`navig tt comments` / `post --comments` now return real comments.** The browser tier could
  reach TikTok's post but comments came back empty. Three root causes, all fixed:
  1. **Headless returns an empty comment body.** TikTok's anti-bot serves an empty `/api/comment/
     list/` response (HTTP 200, 0 bytes) to a *headless* browser even when logged in — verified
     across profiles and read methods. The fetch now runs **headful but offscreen + muted**
     (`--window-position=-2400,-2400 --mute-audio`): a real window that defeats headless detection
     yet is never seen or heard. (Headful is used only when comments are requested; a description-
     only read stays headless/fast.)
  2. **Wrong layout.** The comment side-panel + its "Comments" tab only render at a **desktop width**
     (≥~1024px); the browser's default narrow window gave the immersive layout with no tab. The
     fetch now forces a **1280×1000** window.
  3. **Wrong default tab.** TikTok's side-panel defaults to "You may like", so the comment API never
     fires. The fetch now **dismisses the EU cookie-consent banner** (a nested shadow-DOM component)
     and **clicks the "Comments" tab** (real mouse click on the visible label), then paginates the
     **comment-list container** (not the video feed).
- Requires a logged-in session (`navig tt login`) for EU-gated comments; the vaulted session is
  restored into the fetch browser automatically.

## Unreleased — 2026-07-10

### Added — `--from-browser chrome` beats App-Bound Encryption via a CDP capture fallback
- **`navig tt login --from-browser chrome/edge/brave`** now falls back to a **CDP capture** when
  the direct cookie read fails on Chrome 127+ App-Bound Encryption (the "Failed to decrypt with
  DPAPI" wall). It copies a **minimal** profile slice (Local State + the Cookies DB + prefs — never
  History/passwords) to a throwaway dir and lets the **real** browser decrypt its *own* cookies
  over CDP (ABE keys bind to the user + binary, not the profile path). Never touches your live
  browser/profile; only TikTok's session cookies are stored; the copy is deleted immediately.
  Firefox stays the direct plaintext path. *Note:* the ABE-clone decrypt is machine-dependent — on
  some setups Chrome refuses to decrypt a copied profile, so this degrades cleanly to a clear hint
  (use Firefox, or the real-Chrome interactive login).

### Added — `navig tt login` falls back to a REAL Chrome when the automation engines fail
- **`navig tt login` (`--engine auto`) now escalates: Camoufox → Firefox → a real Chrome over
  CDP.** When the stealth engines get bot-walled or crash (TikTok flags automation and the browser
  dies), the ladder opens a **genuine system Chrome** launched with only a debug port — *no*
  `--enable-automation`, so `navigator.webdriver` is **false** and it looks like a normal browser
  (the one thing that logs in when your regular Chrome does). Live-verified: loads TikTok's login
  page with `webdriver=false` and 0 region failures. It uses its **own** isolated profile — never
  your real Chrome — and cleans up by killing only the process it launched. Also selectable
  directly with `--engine chrome`.
- Escalation only happens when the **browser itself** fails to run; a genuine rate-limit / timeout
  (the browser worked, login didn't) stops the ladder — another engine won't beat a rate lock.

### Added — `navig tt login` explains TikTok's rate limit instead of a silent timeout
- **When TikTok shows "Maximum number of attempts reached"** during login, `navig tt login` now
  detects it on the page and **stops waiting with a clear explanation** (it's a ~24h rate lock —
  wait and retry, or import a session with `--from-browser firefox`), instead of polling in silence
  and ending with a generic "didn't detect a login" after the timeout. Also detects "too many
  attempts". The timeout message now nods to the rate-limit possibility too.

### Fixed — a clear message for posts the browser couldn't read
- **`navig tt post` / `info` / `analyse` now say "Couldn't read this post"** (private, deleted,
  region-blocked, or a page that didn't render) instead of printing a bare, confusing `🎵 TikTok`
  card with "No comments loaded". Adds `_looks_unreadable()` (no id/description/images/stats — the
  URL-derived `uploader` doesn't count) and a hint (`--headful` / `--login` / `--proxy`); `post`/
  `analyse` also skip the empty comments section. `--json` still emits the raw dict for scripts.

### Fixed — a crashing browser no longer crashes the CLI (login + fetch)
- **`navig tt login` no longer crashes if the browser window is closed or dies mid-navigation.**
  A crashed/closed browser raises `Page.goto: Connection closed while reading from the driver`;
  the login drive had no `except`, so it propagated to navig's crash handler (crash log). Now the
  whole attempt is guarded — it reports cleanly and, under `--engine auto`, **falls back to plain
  Firefox** when Camoufox launches-then-crashes (previously the fallback only covered *launch*
  failures, not mid-drive crashes). Regression-tested against the exact "Connection closed" error.
- **The fetch paths degrade the same way:** `navig tt post/info/analyse` catch a mid-fetch browser
  crash and exit cleanly (metadata hint) instead of crashing; the `comments --browser` ladder's
  Tier-B/C now treat a browser crash like any other tier failure (fall through to the next tier /
  empty) rather than propagating.

### Fixed — `navig tt login` robustness (auto-fallback + a "downloading" notice)
- **`--engine auto` no longer hard-fails when Camoufox can't start.** It now degrades to plain
  Firefox (which still beats TikTok's region wall) instead of aborting the login. An explicit
  `--engine camoufox` still fails loudly (respects your choice) rather than silently switching.
- **The one-time ~150MB Camoufox binary download is announced** before it blocks the command,
  so `navig tt login` no longer appears to hang on first use.
- Noted in `browser_fetch._make_controller` that the fetch tier stays Chromium on purpose — a
  Camoufox/Firefox fetch was verified to return empty where Chromium reads the post fully
  (making the Camoufox *fetch* work is a tracked follow-up; login already uses Camoufox).

### Changed — `navig tt login` prefers Camoufox (hides `navigator.webdriver`) via `--engine auto`
- **`navig tt login --engine auto`** (the new default) uses **Camoufox** when its package is
  installed, else plain Firefox. Why it matters: plain Playwright Firefox beats TikTok's *region*
  wall but still leaks `navigator.webdriver=true`, so TikTok can flag it as automation and hit it
  with *"Maximum number of attempts reached"*. Camoufox hides `navigator.webdriver` at the C++
  engine level (Playwright can't override it), so the automated browser looks human. Live-verified
  on TikTok's login page: Camoufox → `navigator.webdriver=false`, zero region failures, gets the
  `msToken`/`ttwid`/`tt_csrf_token` cookies. Enable it with `pip install camoufox[geoip]` (navig
  fetches the ~150MB binary on first use); `--engine firefox`/`chromium` still available.

### Changed — `navig tt login` opens FIREFOX by default (defeats the automated-login bot-wall)
- **`navig tt login` now uses a non-CDP Firefox browser** instead of Chromium. TikTok bot-walls
  automated logins by fingerprinting the CDP/Chromium automation surface — its region check dies
  with `net::ERR_FAILED` → *"internal server error"* in an automated Chrome. Firefox is driven
  over Firefox's Juggler protocol (no CDP artifacts), so it slips past that wall. Live-verified:
  loading TikTok's login page in Firefox produced **zero** failed region/telemetry requests and
  fetched the `msToken`/`ttwid`/`tt_csrf_token` cookies Chromium couldn't.
- `--engine <firefox|camoufox|chromium>` picks the login engine (default `firefox`; `camoufox`
  = a C++-stealth Firefox; `chromium` = the old Patchright path). The login also **warms the
  profile** (a plain visit seeds region cookies) before the login page, and each engine keeps a
  persistent profile across runs. Firefox's binary is auto-provisioned on first use.

### Added — cross-platform music-link resolver (`navig download music-links`)
- **`navig download music-links <url>`** (alias `navig dl music-links`) — paste a Spotify /
  Apple Music / YouTube Music / Deezer / Tidal / SoundCloud link and get the same track's link
  on all 18+ platforms. Uses the free, keyless **song.link (Odesli)** API. `--json` for
  agents; `--country` sets the store market.
- New pure module `navig_download/music_links.py` (`find_music_url` / `resolve_links` /
  `parse_odesli`) — transport-free so the CLI, the Telegram channel, and the agent can all
  reuse it. **Migrated from the retired `core/packages/telegram-bot-navig` pack** — the one
  capability there with no native home; the legacy pack system was removed in the same change.

### Added — import a TikTok session from your browser (`navig tt login --from-browser`)
- **`navig tt login --from-browser <firefox|chrome|edge|brave|…>`** — when TikTok's automated
  web-login is bot-walled (its region check fails with `net::ERR_FAILED` → *"internal server
  error"*), import the session from a browser you're **already** logged into instead. Reads only
  TikTok cookies (never a password) via yt-dlp's cross-browser cookie extraction and vaults
  them; then every browser-tier command auto-uses it. `--profile <name>` picks a browser
  profile. **Firefox is the most reliable on Windows** — Chrome/Edge/Brave must be fully closed
  (their cookie DB locks while open) and may resist app-bound encryption.

## Unreleased — 2026-07-09

### Fixed — `navig tt login` now actually works (session auto-restore)
- **The vaulted session is finally used.** `navig tt login` saved the session under a vault
  slot (`web-session/tiktok.com/tiktok`) that the restore path never read — restore reads the
  *default* slot — so `--login` silently did nothing and gated comments/photos stayed empty.
  Login now saves to the exact slot restore reads, so one login sticks. Regression-tested.

### Changed — logged-in is the default (no `--login` needed), plus `navig tt logout`
- **After `navig tt login`, every browser-tier command uses the session automatically** —
  `navig tt post` / `comments --browser` / `info` / `download` (and the `analyse` photo
  fallback) restore your saved TikTok session with no flag, so EU login-gated comments and
  private/gated photo posts just work. Pass **`--anon`** to force an anonymous fetch; `--login`
  still forces it explicitly. The `--login` flag is now tri-state (`--login/--anon`, default auto).
- **`navig tt logout`** — forget the vaulted session and go back to anonymous browsing.
- Clearer "none loaded" guidance: distinguishes *"run `navig tt login`"* (no session saved)
  from *"your session may have expired"* (a session is present but returned nothing).

### Added — download TikTok photo carousels (yt-dlp can't)
- **`navig tt download` / `batch` now handle `/photo/` posts** — mixed lists auto-route:
  videos via yt-dlp, **photo carousels via the browser**. Images land in
  `<out>/<creator>/<id>/NN.jpg` + a `metadata.json` (description/stats/URLs). `--login` for
  private/gated posts. New `engine.download_photo` / `is_photo_url`; CDN images fetched with
  `curl_cffi` impersonate (+ Referer). Live-proven: a 10-image carousel downloaded (valid
  JPEGs) with full metadata. Note: image URLs are signed + short-lived, so they're fetched
  immediately.
- **Batch photo downloads reuse ONE browser** (`engine.download_photos`) — a multi-photo
  list opens a single stealth session instead of one launch per post (the read-only guard is
  now idempotent so reused pages don't stack `page.route` handlers). Live-proven: a 2-post
  batch used a single browser launch.

### Added — TikTok headless fetcher for photo posts + descriptions (yt-dlp can't)
- **`navig tt post <url>`** — the all-in-one headless reader: opens the post in the stealth
  browser once and returns **description + author + stats + image URLs + top comments**.
  Works for **`/photo/` carousel posts**, which yt-dlp doesn't support at all. `--json` for
  agents, `--headful`, `--login`, `--proxy`.
- **`navig tt info <url>` now handles `/photo/`** — auto-routes photo URLs (and any yt-dlp
  block) through the browser; prints description, stats, and all image URLs. Live-proven:
  a real `/photo/` post returned the full description, 10 image URLs, and 4.7M/768K/962
  view/like/comment stats headless.
- **`navig tt login`** — opens a real browser, waits for you to log into TikTok, and vaults
  the **session** (cookies only — never the password). Then `--login` on `post`/`comments`
  restores it so EU **login-gated comments** come through.
- Browser reader (`tiktok/browser_fetch.py`) gained `fetch_post` — reads the item struct
  from the SSR blob, or (anonymous/EU) falls back to **og-meta + the URL path** for
  description/author/id/cover, and **enriches stats + full image list** from the intercepted
  item-detail JSON. `fetch_comments` is now a thin wrapper over it.

### Added — TikTok anti-detection / self-healing comment fetch
- **Real Chrome identity on the yt-dlp path** — `curl_cffi` `impersonate="chrome"` (real
  TLS/JA3 + HTTP2 fingerprint), plus a coherent rotating UA/headers and optional
  proxy/cookies, now ride **both** the metadata/comments engine (`tiktok/engine.py:_ydl`)
  and the bulk downloader (`downloader/main.py`). New shared `navig_download/anti_detect.py`.
  New dep: `curl_cffi>=0.7`. Coherence guard: when impersonate is active a conflicting UA is
  dropped so `sec-ch-ua` matches the JA3.
- **Block-vs-empty honesty** — `info_with_comments` now flags `comments_blocked=True` when a
  video reports comments but TikTok's signed endpoint serves none; `TikTokBlocked` is raised
  on a hard bot-wall. `navig tt comments/info/analyse` say *"gated (signed endpoint)"* + exit
  2 instead of a misleading "No comments available". New `--proxy` / `--cookies` /
  `--cookies-from-browser` flags on those commands.
- **Tier-B stealth-browser comments** (`tiktok/browser_fetch.py`) — opens the video in the
  stealth browser and intercepts TikTok's **own** signed `/api/comment/list/` JSON (no
  X-Bogus/msToken reversing); dismisses the EU cookie-consent banner; read-only guard blocks
  engagement mutations. Exposed as `navig tt comments --browser [--headful] [--no-cloud]`.
- **Self-healing tier ladder** (`tiktok/fetch.py`) — `navig tt comments --browser` runs
  yt-dlp → stealth browser → cloud, auto-escalating on block/empty; on a browser-tier win it
  records the signed endpoint **shape** (path + param/header NAMES only, never secret values)
  into the core signer cache so signing bumps can't permanently break the fast path.

### Notes
- Canonical anti-detection helpers live in `navig_download/anti_detect.py`; the reusable
  browser blocks (proxy pool, interceptor, fingerprint, cloud/clearcote engines, pacing,
  persona, signer cache) live in **navig-core** `navig/browser/*` and are shared across the
  whole headless stack. curl_cffi/patchright degrade gracefully if absent.

## 0.1.0 — 2026-07-07

### Added
- **Extracted from `navig-media`** — the yt-dlp download engine (`downloader/` + the
  TikTok engine facade `tiktok/`) is now its own standalone plugin.
- **`navig download` / `dl`** — new primary command namespace for the (site-generic)
  downloader. `navig tiktok` / `tt` keep working as back-compat aliases of the same app;
  every existing subcommand (download/batch/profile/info/comments/analyse) is unchanged.

### Notes
- The download/metadata engine is byte-for-byte the `navig-media` code, re-homed under
  `navig_download`. Deps (`yt-dlp`, `tqdm`, `urllib3`, `requests`) travel with this plugin
  only — so publishing-only installs no longer pull yt-dlp.

### Follow-up
- Make `navig download <url>` a top-level default (URL as the primary argument) and broaden
  the TikTok-specific naming to reflect multi-site support.
