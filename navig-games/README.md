# navig-games

**Find & auto-claim FREE games** across storefronts — surfaced as `navig games`.
A first-party NAVIG plugin (free, toggleable). Phase 1 ships the **Epic Games
Store** end-to-end; GOG, Amazon Prime Gaming and a Steam suite follow.

> **FREE-ONLY — never spends money.** The claim engine only ever completes a **$0**
> checkout. Any title with a real price is refused. Three independent gates
> enforce this: (A) the store API reports a $0 discounted price, (B) the store CTA
> reads "Get" (Epic never shows "Get" for a priced game), and (C) the checkout
> overlay total is re-read and must not show a positive amount before the order is
> placed. Any parse failure resolves toward *refuse*.

## Quick start

```bash
# Free games (Epic)
navig games check                 # what's free right now (+ --upcoming, --json)
navig games login epic            # one-time sign-in; session saved to the vault
navig games claim --dry-run       # verify free & stop before ordering (proof)
navig games claim                 # claim the current freebies (confirm prompt)
navig games schedule enable       # auto-claim on a schedule (daily by default)

# Library (Steam / Epic / GOG / Amazon)
navig games library               # every installed game across your launchers
navig games unify                 # add your games to Steam (shortcuts.vdf) + cover art
navig games art <game>            # fetch/refresh Steam cover art (--all, --portrait, …)
navig games art --set-key <key>   # save a SteamGridDB key for full portrait grids
navig games deals                 # price drops + free-to-keep on your Steam wishlist
navig games schedule enable --job deals    # daily deal alerts (notify)
navig games schedule enable --job unify    # keep Steam synced with new installs
navig games doctor                # full self-check across every subsystem
navig games steam auth --mafile x # store a Steam Guard secret in the vault
navig games steam code            # current Steam Guard 2FA code

navig games history | status | doctor
```

## How it works

| Layer | What it does | Reuses (navig-core) |
|---|---|---|
| Sourcing | Epic's public `freeGamesPromotions` API → current/upcoming free games | `requests` |
| Claim | Persistent, isolated browser → session-first login → $0 checkout | `navig.browser.cdp_actions`, `autofill`, `session_manager` |
| Vault | Store the login session once, restore it thereafter | `navig.vault.sessions` / `logins` / `totp` |
| Ledger | Idempotent record of claimed games (never claim twice) | `navig.platform.paths` |
| Schedule | Recurring auto-claim job | core `CronService` (`cron_jobs.json`) |
| Notify | "Claimed X" fan-out to Telegram / desktop / deck | `navig.notify.dispatch` |
| Library | Scan installed games from launcher manifests (no login) | Windows registry / VDF / SQLite |
| Unify | Add non-Steam games to Steam (`shortcuts.vdf`, backup + dedupe) | own binary-VDF codec |
| Cover art | Steam grid art for unified games (GOG-local keyless · SteamGridDB opt) | `requests` |
| Deals | Wishlist price-drop + free-to-keep alerts (keyless Steam APIs, alert-once) | sourcing spine + `navig.notify` |
| Steam Guard | 5-char 2FA code from a vaulted `shared_secret` | `navig.vault` |
| Agent | `games_check` · `games_library` · `games_deals` (read-only) · `games_claim` (owner-only) | `navig.tools.BaseTool` |
| Deck API | `/api/deck/games/{status,free,deals,library}` for deck/OS/remote | `gateway:register_routes` hook (aiohttp) |

## Design notes

- The Epic claim flow and the Steam-library discovery are **implemented natively** —
  first-party code end-to-end, no third-party engine imported.
- First run requires `navig games login epic` once (Epic mandates a real sign-in);
  after that, claims restore the vaulted session with no password typed.
- Captcha / un-handleable 2FA stops the run and notifies "needs you" — it never
  loops or guesses.

## Roadmap

- **Phase 1 — Epic free-game auto-claim** ✅
- **Phase 4 — unify launchers into Steam · Steam Guard TOTP · cover art · wishlist deals watcher** ✅
- Phase 2 GOG free-game claim · Phase 3 Amazon Prime Gaming claim
- Phase 4 (rest) — Steam achievements · cloud-save sync
- Phase 5 — deck/OS "Games" dashboard (free-game feed + deals + one-click unify)
