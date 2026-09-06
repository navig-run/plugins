"""EmailService — the proactive email tick: filter→notify + scheduled briefings.

Runs inside the notify scheduler loop (one shared daemon loop). Everything is
best-effort and never raises into the loop. Delivery goes through the unified
notify router so it lands in the deck bell-feed + Telegram per the user's
Settings → Notifications matrix.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from navig_email import config as cfg
from navig_email import gmail, rules

logger = logging.getLogger("navig_email")

_SEEN_CAP = 300


def _cadence_query(cadence: str) -> str:
    return {"daily": "newer_than:1d", "weekly": "newer_than:7d", "monthly": "newer_than:30d"}.get(
        cadence, "newer_than:1d"
    )


def _period_key(cadence: str, now: datetime) -> str:
    if cadence == "weekly":
        iso = now.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    if cadence == "monthly":
        return now.strftime("%Y-%m")
    return now.strftime("%Y-%m-%d")


def _due(b: dict[str, Any], now: datetime, last: str | None) -> bool:
    if not b.get("enabled", True):
        return False
    if now.hour != int(b.get("hour", 8)):
        return False
    cadence = b.get("cadence", "daily")
    if cadence == "weekly" and now.weekday() != int(b.get("weekday", 0)):
        return False
    if cadence == "monthly" and now.day != int(b.get("day", 1)):
        return False
    return last != _period_key(cadence, now)


async def _dispatch(type_key: str, title: str, body: str, channels: list[str] | None, priority: str = "normal", data: dict | None = None) -> bool:
    """Deliver via the notify router. Returns True only on a real dispatch — a caller that
    records "sent"/marks a period complete MUST honor this, or a failed delivery becomes a
    phantom success that's never retried (the reminder delivery-integrity class)."""
    try:
        from navig.notify.router import get_notification_router

        await get_notification_router().dispatch(
            type_key, title, body, priority=priority, data=data or {}, only_channels=channels or None
        )
        return True
    except Exception:
        logger.debug("email_ops dispatch failed", exc_info=True)
        return False


async def build_brief(briefing: dict[str, Any]) -> str:
    """Compose an 'important & interesting' brief of the briefing's emails."""
    cadence = briefing.get("cadence", "daily")
    label = (briefing.get("label") or "").strip()
    query = (briefing.get("query") or "").strip() or (
        (f"label:{label} " if label else "") + _cadence_query(cadence)
    )
    msgs = await gmail.search(query, limit=25)
    if not msgs:
        return ""
    lines = []
    for m in msgs[:25]:
        lines.append(f"- From {m['from']}: {m['subject']} — {m['snippet'][:160]}")
    digest_src = "\n".join(lines)
    focus = (briefing.get("focus") or "").strip()
    try:
        from navig.llm.generate import llm_generate

        sys = (
            "You are a sharp email briefer. From the email list, write a short brief of what's "
            "IMPORTANT and INTERESTING: 3-6 bullet lines, group similar items, flag anything that "
            "needs a reply or a deadline. Start each line with '- '. No preamble."
        )
        if focus:
            sys += f" Focus: {focus}."
        out = await asyncio.to_thread(
            llm_generate,
            messages=[{"role": "system", "content": sys}, {"role": "user", "content": digest_src[:6000]}],
            mode="summarize", temperature=0.4, max_tokens=400,
        )
        text = (out or "").strip()
        return text or digest_src
    except Exception:
        logger.debug("email_ops brief llm failed; raw list", exc_info=True)
        return digest_src


class EmailService:
    def __init__(self) -> None:
        self.last_check: datetime | None = None
        self.last_error: str | None = None

    async def run_brief_now(self, briefing_id: str | None = None) -> dict[str, Any]:
        """Build + dispatch a briefing immediately (the 'Send now' button).
        If no id, uses the first enabled briefing or a default daily brief."""
        c = cfg.load_config()
        briefs = c.get("briefings") or []
        b = next((x for x in briefs if x.get("id") == briefing_id), None) if briefing_id else None
        if b is None:
            b = next((x for x in briefs if x.get("enabled", True)), None) or {"cadence": "daily", "name": "Daily email brief", "channels": ["deck"]}
        text = await build_brief(b)
        if not text:
            return {"ok": True, "sent": False, "note": "No emails to brief on."}
        title = f"📧 {b.get('name') or 'Email brief'} · {datetime.now().strftime('%a %d %b')}"
        # Report what actually happened — a swallowed dispatch failure must not read as sent.
        delivered = await _dispatch("briefing", title, text, b.get("channels"))
        result = {"ok": delivered, "sent": delivered, "text": text}
        if not delivered:
            result["note"] = "brief built but delivery failed — check Settings → Notifications"
        return result

    async def tick(self, gateway=None) -> None:
        """One pass: filter→notify on new mail + fire any due briefings."""
        try:
            c = cfg.load_config()
            if not gmail.is_connected():
                return
            await self._monitor(c)
            await self._briefings(c)
            self.last_check = datetime.now()
            self.last_error = None
        except Exception as exc:  # noqa: BLE001 — never break the scheduler loop
            self.last_error = str(exc)
            logger.debug("email_ops tick failed", exc_info=True)

    async def _monitor(self, c: dict[str, Any]) -> None:
        rule_list = [r for r in (c.get("rules") or []) if r.get("enabled", True)]
        if not c.get("monitor_enabled", True) or not rule_list:
            return
        state = c.get("state") or {}
        seen: list[str] = list(state.get("seen_ids") or [])
        seen_set = set(seen)
        seeded = bool(state.get("seeded"))

        msgs = await gmail.search("newer_than:2d", limit=20)
        if rules.needs_body(rule_list):
            for m in msgs:
                if m["id"] not in seen_set and not m.get("body"):
                    m["body"] = await gmail.fetch_body(m["id"])

        new_ids: list[str] = []       # every new email this tick — the first-run seed set
        handled_ids: list[str] = []   # fully handled: no rule matched, or the alert was delivered
        for m in msgs:
            if m["id"] in seen_set:
                continue
            new_ids.append(m["id"])
            if not seeded:
                continue  # first run: seed, don't notify the backlog
            rule = rules.first_match(m, rule_list)
            if not rule:
                handled_ids.append(m["id"])  # nothing to deliver → handled
                continue
            title = f"📧 {m['subject'] or '(no subject)'}"
            body = f"From {m['from']}\n\n{(m.get('snippet') or '')[:500]}"
            if await _dispatch(
                "email_important", title, body, rule.get("channels"),
                priority="high", data={"url": m.get("url", ""), "rule": rule.get("name", "")},
            ):
                handled_ids.append(m["id"])  # delivered → handled
            # else: delivery failed → leave UNSEEN so the next tick retries the alert
            # (verified-delivery class) instead of dropping the important email forever.

        if not seeded:
            # First run seeds ALL current mail as seen (never notify the backlog) and flips the
            # flag even on an empty inbox, so the next tick treats arrivals as notifiable.
            merged = (new_ids + seen)[:_SEEN_CAP]
            cfg.update_state(seen_ids=merged, seeded=True)
        elif handled_ids:
            # Seeded run: mark seen ONLY what was handled — an alert whose dispatch FAILED stays
            # unseen and is retried next tick (bounded by the newer_than:2d fetch window).
            merged = (handled_ids + seen)[:_SEEN_CAP]
            cfg.update_state(seen_ids=merged)

    async def _briefings(self, c: dict[str, Any]) -> None:
        briefs = [b for b in (c.get("briefings") or []) if b.get("enabled", True)]
        if not briefs:
            return
        now = datetime.now()
        last_brief: dict[str, str] = dict((c.get("state") or {}).get("last_brief") or {})
        changed = False
        for b in briefs:
            bid = b.get("id") or b.get("name") or ""
            if not _due(b, now, last_brief.get(bid)):
                continue
            text = await build_brief(b)
            if not text:
                # No emails to brief on — nothing to deliver; mark the period done so we
                # don't rebuild an empty brief every tick.
                last_brief[bid] = _period_key(b.get("cadence", "daily"), now)
                changed = True
                continue
            title = f"📧 {b.get('name') or 'Email brief'} · {now.strftime('%a %d %b')}"
            if await _dispatch("briefing", title, text, b.get("channels")):
                # Mark the period complete ONLY on verified delivery. A failed dispatch
                # leaves it unmarked, so the next tick within the configured hour retries
                # (rather than silently dropping the brief for the whole day/week/month).
                last_brief[bid] = _period_key(b.get("cadence", "daily"), now)
                changed = True
        if changed:
            cfg.update_state(last_brief=last_brief)


_service: EmailService | None = None


def get_email_service() -> EmailService:
    global _service
    if _service is None:
        _service = EmailService()
    return _service
