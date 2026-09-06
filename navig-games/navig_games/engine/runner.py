"""High-level orchestration used by both the CLI and the agent tool.

``check`` = pure sourcing (no browser). ``run_claim`` = source → filter unseen via
the ledger → drive the browser (FREE-ONLY) → record results → notify. Exposed as
an async coroutine plus a sync wrapper (``run_claim_sync``) that spins core's CDP
event loop.
"""

from __future__ import annotations

import logging
from typing import Callable

from . import last_run
from . import notify as notif
from . import settings
from .claim.base import (
    STATUS_CLAIMED,
    STATUS_DRYRUN,
    STATUS_GRABBED,
    STATUS_MANUAL,
    STATUS_OWNED,
)
from .ledger import Ledger
from .models import ENDING_SOON_DAYS, FreeGame, days_until

_log = logging.getLogger(__name__)

_STORES = ("all", "epic", "steam", "giveaways")


def _source(
    store: str, country: str, locale: str
) -> tuple[list[FreeGame], list[FreeGame]]:
    """Source (current, upcoming) free games. No browser, no login, never raises.

    Single source of truth for every read path (``check``, ``mark_grabbed``) so the
    three feeds can't drift apart. Each feed is isolated: one failing must never
    take the others down.
    """
    current: list[FreeGame] = []
    upcoming: list[FreeGame] = []

    if store in ("all", "epic"):
        from .sources import epic as epic_src

        data = epic_src.fetch_free_games(country=country, locale=locale)
        current += data["current"]
        upcoming += data["upcoming"]

    if store in ("all", "steam"):
        try:
            from .sources import steam as steam_src

            current += steam_src.free_to_keep(cc=country.lower())
        except Exception as exc:  # noqa: BLE001 — a Steam hiccup must not break the Epic feed
            _log.warning("games: steam free-to-keep sourcing failed (%s)", exc)

    if store in ("all", "giveaways"):
        try:
            from .sources import giveaways as gw_src

            current += gw_src.fetch()
        except Exception as exc:  # noqa: BLE001 — one feed must never break the others
            _log.warning("games: cross-store giveaway sourcing failed (%s)", exc)

    return current, upcoming


def check(store: str = "all", country: str | None = None, locale: str | None = None) -> dict:
    """Source current + upcoming free games across supported stores. No browser, no login.

    ``store``: ``all`` (default) | ``epic`` | ``steam`` | ``giveaways`` (the
    cross-store feed: itch / GOG / IndieGala / Ubisoft …). Only Epic is
    auto-claimable — everything else is surfaced to grab in its own store and never
    enters the claim path.
    """
    country = (country or settings.country()).upper()
    locale = locale or settings.locale()
    if store not in _STORES:
        return {"store": store, "current": [], "upcoming": [],
                "error": f"store {store!r} not supported ({' | '.join(_STORES)})"}

    current_games, upcoming_games = _source(store, country, locale)

    # Tag each freebie with what the ledger already knows about it, so the Free tab
    # can show "owned"/"claimed"/"got it" vs offer a Claim.
    ledger = Ledger()

    def _tag(game: FreeGame) -> dict:
        d = game.to_dict()
        rec = ledger.get(game.key)
        d["claim_status"] = rec.get("status") if rec else None
        d["ends_in_days"] = days_until(game.ends_at)  # None = open-ended / unknown
        return d

    current = [_tag(g) for g in current_games]
    # Grab-before-it's-gone order: ending soonest first, open-ended (None) last.
    # Stable, so the per-source order is preserved within the same day-count.
    current.sort(key=lambda d: (d.get("ends_in_days") is None, d.get("ends_in_days") or 0))
    upcoming = [g.to_dict() for g in upcoming_games]

    return {
        "store": store,
        "country": country,
        "locale": locale,
        "current": current,
        "upcoming": upcoming,
    }


def mark_grabbed(
    key: str,
    *,
    grabbed: bool = True,
    country: str | None = None,
    locale: str | None = None,
) -> dict:
    """Mark a free game as "I got it myself" — or undo that.

    The terminal state for everything we can't claim headlessly (itch / IndieGala /
    Stove / GOG / Steam free-to-keep): without it those rows have no way to ever
    settle, so they'd resurface in the Free tab and sit in History as an amber
    "needs you" forever.

    Marking an *Epic* game doubles as "skip this one" — the scheduled claim honours
    the ledger — while a deliberate per-game claim still overrides it
    (``run_claim(only=…)`` never consults the ledger).

    Refuses to un-mark anything we actually claimed or that the store reported as
    owned: that is real history, not a user note.
    """
    country = (country or settings.country()).upper()
    locale = locale or settings.locale()
    ledger = Ledger()
    rec = ledger.get(key)

    if not grabbed:
        if rec is None:
            return {"key": key, "grabbed": False, "changed": False,
                    "message": "not marked"}
        if rec.get("status") != STATUS_GRABBED:
            return {"key": key, "grabbed": False, "changed": False,
                    "status": rec.get("status"),
                    "error": "that game was claimed or already owned — history, not a note"}
        ledger.forget(key)
        return {"key": key, "title": rec.get("title", ""), "grabbed": False,
                "changed": True, "message": "unmarked"}

    if rec and rec.get("status") in (STATUS_CLAIMED, STATUS_OWNED):
        return {"key": key, "title": rec.get("title", ""), "grabbed": True,
                "changed": False, "status": rec.get("status"),
                "message": "already in your library"}

    current, _ = _source("all", country, locale)
    game = next((g for g in current if g.key == key), None)
    if game is None:
        return {"key": key, "grabbed": False, "changed": False,
                "error": "that game isn't free right now"}

    ledger.record(game, STATUS_GRABBED, note="you marked it as grabbed")
    return {"key": key, "title": game.title, "store": game.store, "grabbed": True,
            "changed": True, "status": STATUS_GRABBED, "message": "marked as grabbed"}


async def run_claim(
    *,
    store: str = "epic",
    dry_run: bool = False,
    force: bool = False,
    only: str | None = None,
    country: str | None = None,
    locale: str | None = None,
    require_overlay_zero: bool | None = None,
    on_status: Callable[[str], None] | None = None,
    do_notify: bool = True,
) -> dict:
    """Source, then claim the currently-free games. FREE-ONLY.

    ``only`` = a single game key: claim just that game (regardless of whether the
    ledger has already seen it), for the Free tab's per-game Claim button.
    """
    country = (country or settings.country()).upper()
    locale = locale or settings.locale()
    if require_overlay_zero is None:
        require_overlay_zero = settings.coerce_bool(settings.get("require_overlay_zero", False))

    if store != "epic":
        return {"store": store, "error": "only Epic is supported in v1", "results": []}

    from .sources import epic as epic_src

    current = epic_src.fetch_free_games(country=country, locale=locale)["current"]
    by_key: dict[str, FreeGame] = {g.key: g for g in current}

    ledger = Ledger()
    if only:
        targets = [g for g in current if g.key == only]
    else:
        targets = current if force else ledger.unseen(current)

    if not targets:
        if only:
            message = "that game isn't free right now"
        elif current:
            message = "nothing new to claim"
        else:
            message = "no free games right now"
        summary = {
            "store": store, "country": country, "checked": len(current),
            "attempted": 0, "claimed": 0, "dry_run": dry_run, "results": [],
            "message": message,
        }
        last_run.record(summary)
        return summary

    from .claim.epic import run_epic_claims

    results, login = await run_epic_claims(
        targets, dry_run=dry_run, require_overlay_zero=require_overlay_zero,
        on_status=on_status,
    )

    # A dead/absent session makes run_epic_claims bail before signing in, so EVERY
    # game comes back "needs you: not signed in". That's one root cause, not N — so
    # it collapses into a single deduped "sign-in expired" alert below, instead of a
    # per-game ping on every daily scheduled run. Genuine per-game manuals (region
    # lock, captcha, add-on-needs-base) only occur once we DID sign in, so they keep
    # their individual alerts.
    session_expired = login == "needs_manual"
    signed_in = login in ("session_restored", "filled", "logged_in", "cookies")

    claimed = []
    for res in results:
        if res.status != STATUS_DRYRUN:
            game = by_key.get(res.game_key)
            if game is not None:
                ledger.record(game, res.status, res.message)
        if res.status == STATUS_CLAIMED:
            claimed.append(res)
        if do_notify and not session_expired:
            await notif.notify_result(res)
    if do_notify and not session_expired:
        await notif.notify_summary(claimed)

    # Proactive Epic sign-in alert — one high-signal, alert-once nudge for the
    # scheduled path. A dry-run is a check (the user is watching), not the real
    # unattended claim, so it never alerts. Never let a health note break a claim.
    if not dry_run:
        try:
            from .login_state import LoginAlertState

            state = LoginAlertState()
            if session_expired:
                keys = [r.game_key for r in results]
                if do_notify and state.should_alert_expiry(keys):
                    await notif.notify_session_expired(len(results))
                    state.mark_expiry_alerted(keys)
            elif signed_in:
                state.clear_expiry()  # re-arm so the next expiry alerts again
        except Exception as exc:  # noqa: BLE001 — telemetry, never a blocker
            _log.warning("games: sign-in alert bookkeeping failed (%s)", exc)

    summary = {
        "store": store, "country": country, "checked": len(current),
        "attempted": len(targets), "claimed": len(claimed),
        "dry_run": dry_run, "login": login, "results": [r.to_dict() for r in results],
    }
    last_run.record(summary)
    return summary


def run_claim_sync(**kwargs) -> dict:
    """Sync wrapper for the CLI — runs :func:`run_claim` on core's CDP loop."""
    from navig.browser.cdp_runtime import run

    return run(run_claim(**kwargs))


def run_deals_notify(*, threshold: int = 20, cc: str = "us", include_wishlist: bool = True) -> dict:
    """Check Steam wishlist/watchlist deals and notify anything new (alert-once).

    Sync; safe for the CLI and the scheduled subprocess. Never raises.
    """
    import asyncio

    from .deals_state import DealsState
    from .sources import steam as steam_src

    # Alerting must never fire on stale cached prices — always fetch fresh.
    r = steam_src.check_deals(threshold=threshold, cc=cc, include_wishlist=include_wishlist,
                              use_cache=False)
    deals = r["deals"]
    state = DealsState()
    fresh = [d for d in deals if state.should_alert(d)]
    if fresh:
        try:
            asyncio.run(notif.notify_deals(fresh))
        except Exception as exc:  # noqa: BLE001 — notify best-effort
            _log.warning("games: deal notify failed (%s)", exc)
        for d in fresh:
            state.mark(d)
    state.reset_absent([d.appid for d in deals])
    state.save()

    # Giveaways we can't auto-claim: Steam free-to-keep (store-wide, not just your
    # wishlist) + the cross-store feed (itch / GOG / IndieGala / …). First-seen dedupe
    # via the ledger, so each is announced once rather than on every run.
    fresh_giveaways: list[FreeGame] = []
    try:
        from .sources import giveaways as gw_src

        found: list[FreeGame] = []
        for label, source in (
            ("steam free-to-keep", lambda: steam_src.free_to_keep(cc=cc, use_cache=False)),
            ("cross-store giveaways", lambda: gw_src.fetch(use_cache=False)),
        ):
            try:
                found += source()
            except Exception as exc:  # noqa: BLE001 — one feed must not sink the other
                _log.warning("games: %s sourcing failed (%s)", label, exc)

        ledger = Ledger()
        fresh_giveaways = [g for g in found if ledger.get(g.key) is None]
        if fresh_giveaways:
            asyncio.run(notif.notify_free_to_keep(fresh_giveaways))
            for g in fresh_giveaways:
                ledger.record(g, STATUS_MANUAL,
                              f"free to keep on {g.store} — grab it in the store")
    except Exception as exc:  # noqa: BLE001 — best-effort; never break the deals run
        _log.warning("games: giveaway notify failed (%s)", exc)

    # "Grab it before it's gone": a one-time reminder for giveaways surfaced on an
    # earlier run that you still haven't grabbed and that now end within
    # ENDING_SOON_DAYS. Excludes the ones just announced above (fresh) — they carry
    # their own deadline — and anything already settled. Alert-once so a still-unclaimed
    # freebie isn't a daily drip. Best-effort; never breaks the deals run.
    ending_soon: list[FreeGame] = []
    try:
        from .expiry_state import ExpiryReminderState

        fresh_keys = {g.key for g in fresh_giveaways}
        rem = ExpiryReminderState()
        for g in found:
            if g.key in fresh_keys or ledger.is_settled(g.key):
                continue
            d = days_until(g.ends_at)
            if d is None or d > ENDING_SOON_DAYS:
                continue
            if rem.should_remind(g.key):
                ending_soon.append(g)
        if ending_soon:
            asyncio.run(notif.notify_ending_soon(ending_soon))
            for g in ending_soon:
                rem.mark(g.key)
        rem.prune([g.key for g in found])
        rem.save()
    except Exception as exc:  # noqa: BLE001 — a reminder must never break the run
        _log.warning("games: ending-soon reminder failed (%s)", exc)

    return {"checked": r["checked"], "deals": len(deals), "notified": len(fresh),
            "titles": [d.name for d in fresh],
            "free_to_keep": [g.title for g in fresh_giveaways],
            "ending_soon": [g.title for g in ending_soon]}
