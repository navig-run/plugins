"""Notification fan-out — reuses core's ``navig.notify.dispatch`` so a claim
reaches Telegram / desktop / deck per the user's own preferences.

Uses the ``free_game`` notification type when core defines it (added by the
plugin's optional core edit); otherwise falls back to ``custom``. Never raises —
a notify failure must not break a claim.
"""

from __future__ import annotations

import logging

from .claim.base import STATUS_CLAIMED, STATUS_MANUAL, STATUS_OWNED, STATUS_PRICED

_log = logging.getLogger(__name__)


def _type_key() -> str:
    try:
        from navig.notify.types import TYPE_KEYS

        if "free_game" in TYPE_KEYS:
            return "free_game"
    except Exception:  # noqa: BLE001
        pass
    return "custom"


_ICON = {
    STATUS_CLAIMED: "🎮",
    STATUS_OWNED: "📚",
    STATUS_MANUAL: "⚠️",
    STATUS_PRICED: "💲",
}


async def notify_result(result, store_label: str = "Epic") -> None:
    """Announce a single claim outcome. Best-effort."""
    try:
        from navig.notify import dispatch
    except Exception:  # noqa: BLE001 — notify subsystem unavailable
        return

    icon = _ICON.get(result.status, "🎮")
    verb = {
        STATUS_CLAIMED: "Claimed",
        STATUS_OWNED: "Already owned",
        STATUS_MANUAL: "Needs you",
        STATUS_PRICED: "Skipped (not free)",
    }.get(result.status, result.status)

    title = f"{icon} {verb}: {result.title}"
    body = result.message or f"{store_label} · {result.url}"
    priority = "high" if result.status == STATUS_MANUAL else "normal"
    try:
        await dispatch(
            _type_key(),
            title,
            body,
            priority=priority,
            data={
                "store": result.store,
                "status": result.status,
                "title": result.title,
                "url": result.url,
            },
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("games: notify failed (%s)", exc)


async def notify_summary(claimed: list, store_label: str = "Epic") -> None:
    """A one-line roll-up after a run that claimed one or more games."""
    if not claimed:
        return
    try:
        from navig.notify import dispatch
    except Exception:  # noqa: BLE001
        return
    titles = ", ".join(c.title for c in claimed)
    try:
        await dispatch(
            _type_key(),
            f"🎮 {len(claimed)} free game(s) claimed on {store_label}",
            titles,
            priority="normal",
            data={"count": len(claimed), "titles": [c.title for c in claimed]},
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("games: summary notify failed (%s)", exc)


async def notify_session_expired(count: int, store_label: str = "Epic") -> None:
    """One high-signal alert when a scheduled claim can't sign in — the whole batch
    is blocked on a single re-login, so it collapses to one actionable nudge rather
    than one "needs you" per game. Best-effort; never raises. De-duping (alert-once
    per expired batch) is the caller's job — see ``engine.login_state``.
    """
    try:
        from navig.notify import dispatch
    except Exception:  # noqa: BLE001
        return
    n = max(1, int(count or 0))
    plural = "s" if n != 1 else ""
    try:
        await dispatch(
            _type_key(),
            f"🔑 {store_label} sign-in expired",
            f"{n} free game{plural} waiting — re-authorize once: run `navig games login {store_label.lower()}`",
            priority="high",
            data={"store": store_label.lower(), "reason": "session_expired", "waiting": n},
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("games: session-expired notify failed (%s)", exc)


async def notify_deals(deals: list) -> None:
    """Announce Steam wishlist deals — free-to-keep as a `free_game`, price drops
    as a single roll-up. Best-effort."""
    if not deals:
        return
    try:
        from navig.notify import dispatch
    except Exception:  # noqa: BLE001
        return

    free = [d for d in deals if getattr(d, "is_free", False)]
    drops = [d for d in deals if not getattr(d, "is_free", False)]

    for d in free:  # each free-to-keep game gets its own high-signal alert
        try:
            await dispatch(_type_key(), f"🎁 Free to keep: {d.name}",
                           f"Steam · was {d.currency} {d.initial_cents / 100:.2f} · {d.url}",
                           priority="high",
                           data={"store": "steam", "appid": d.appid, "title": d.name,
                                 "url": d.url, "free": True})
        except Exception as exc:  # noqa: BLE001
            _log.warning("games: deal notify failed (%s)", exc)

    if drops:
        top = drops[:6]
        lines = ", ".join(f"{d.name} -{d.discount_pct}% ({d.final_formatted})" for d in top)
        more = f" +{len(drops) - len(top)} more" if len(drops) > len(top) else ""
        try:
            await dispatch(_type_key(), f"🏷 {len(drops)} wishlist game(s) on sale",
                           lines + more, priority="normal",
                           data={"store": "steam", "count": len(drops),
                                 "deals": [{"appid": d.appid, "name": d.name,
                                            "discount": d.discount_pct} for d in top]})
        except Exception as exc:  # noqa: BLE001
            _log.warning("games: deals summary notify failed (%s)", exc)


async def notify_ending_soon(games: list) -> None:
    """One batched, high-signal 'grab it before it's gone' reminder for un-grabbed
    giveaways about to expire. Time-critical, so it's high priority; batched into a
    single message (soonest first) rather than one ping per game. Best-effort;
    alert-once dedup is the caller's job — see ``engine.expiry_state``.
    """
    if not games:
        return
    try:
        from navig.notify import dispatch
    except Exception:  # noqa: BLE001
        return
    from .models import days_until

    ranked = sorted(((days_until(g.ends_at), g) for g in games),
                    key=lambda t: (t[0] is None, t[0] or 0))
    lines = []
    for d, g in ranked:
        # The caller only sends imminent (parseable) games, but stay honest if not:
        # never render "ends in None days".
        when = ("soon" if d is None
                else "today" if d == 0
                else "tomorrow" if d == 1
                else f"in {d} days")
        was = f" · was {g.original_price}" if g.original_price else ""
        lines.append(f"{g.title} — {g.store}, ends {when}{was}: {g.url}")

    n = len(ranked)
    plural = "s" if n != 1 else ""
    try:
        await dispatch(
            _type_key(),
            f"⏰ {n} free game{plural} ending soon",
            "\n".join(lines),
            priority="high",
            data={"reason": "ending_soon", "count": n,
                  "games": [{"store": g.store, "title": g.title, "url": g.url}
                            for _, g in ranked]},
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("games: ending-soon notify failed (%s)", exc)


async def notify_free_to_keep(games: list) -> None:
    """Announce free-to-keep giveaways that we can't claim headlessly (Steam) — a
    high-signal 'grab it in the store' alert. Best-effort; never raises."""
    if not games:
        return
    try:
        from navig.notify import dispatch
    except Exception:  # noqa: BLE001
        return

    for g in games:
        try:
            was = f" · was {g.original_price}" if g.original_price else ""
            await dispatch(
                _type_key(),
                f"🎁 Free to keep: {g.title}",
                f"{g.store.title()}{was} · add it to your library: {g.url}",
                priority="high",
                data={"store": g.store, "title": g.title, "url": g.url, "free": True},
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("games: free-to-keep notify failed (%s)", exc)
