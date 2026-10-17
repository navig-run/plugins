---
name: games-ops
description: >-
  Find and auto-claim FREE games from the Epic Games Store (GOG, Amazon Prime and
  Steam coming next). Use when the user mentions free games, Epic freebies,
  claiming a weekly free game, or asks what games are free right now. FREE-ONLY —
  never buys a priced game.
activation_keywords:
  - free game
  - free games
  - epic games
  - epic freebie
  - weekly free game
  - claim game
  - game giveaway
  - games this week
metadata:
  type: cli-skill
  surface: games
---

# Free games — find & auto-claim

`navig games` finds new **free** games and claims them into the user's account.
Phase 1 covers the **Epic Games Store**; GOG, Amazon Prime Gaming and a Steam
suite follow.

## Hard rule — FREE-ONLY
Only a **$0** checkout is ever completed. A title with any real price is refused
(three independent gates: the store API says $0, the store button reads "Get",
and the checkout total is re-read before ordering). This never spends money.

## Commands
- `navig games check [--upcoming] [--json]` — what's free right now / next. No login.
- `navig games claim [--dry-run] [--all] [--yes] [--json]` — claim the current freebies.
  `--dry-run` verifies the free total and stops **before** placing the order.
- `navig games login epic [--user <email>]` — one-time sign-in; the session is
  saved to the vault so future claims run unattended.
- `navig games schedule enable [--when daily|weekly]` — auto-claim on a schedule.
- `navig games history` · `navig games status` · `navig games doctor`.

### Library (Steam / Epic / GOG / Amazon)
- `navig games library [--store …] [--json]` — every installed game across launchers (local scan).
- `navig games unify [--dry-run] [--force] [--no-art]` — add your Epic/GOG/Amazon games to
  Steam with cover art (writes shortcuts.vdf; backs up, dedupes, refuses while Steam runs).
- `navig games art [game|--all] [--portrait/--hero/--logo <img>]` — fetch/refresh Steam
  cover art; `--set-key <k>` saves a SteamGridDB key for full portrait grids.
- `navig games deals [--threshold N]` — price drops + free-to-keep on your Steam wishlist
  (+ `deals watch/unwatch/list`, `deals notify`). `schedule enable --job deals` for daily alerts.
- `navig games steam auth (--shared-secret <b64> | --mafile <path>)` — store a Steam Guard secret.
- `navig games steam code` / `steam accounts` — current Steam Guard 2FA code.

## Typical flows
- "What games are free on Epic?" → `navig games check`.
- "Claim this week's free games" → confirm the user is signed in
  (`navig games status`); if not, `navig games login epic`; then
  `navig games claim` (or `--dry-run` first to show it's safe).
- "Grab free games automatically every week" → `navig games login epic` once, then
  `navig games schedule enable --when daily` (daily so a missed day self-heals; the
  ledger dedupes).

## Notes
- First run needs `navig games login epic` once (Epic requires a real sign-in).
- Captcha/2FA that can't be handled unattended → the run stops and notifies
  "needs you" rather than looping or guessing.
