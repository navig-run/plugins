"""Outbox events → navig's notify router (deck feed, Telegram, per the operator's matrix).

Wording follows the state, never the intent: "queued" for SCHEDULED, "sent" only for SENT,
"may have been sent" for OUTCOME_UNKNOWN. Each announcement is claimed once per job version in
navig's cross-process dedupe table, so a restart or a second process never repeats it. Quiet
hours and channel choice belong to the router — this module only says what happened.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import quote

logger = logging.getLogger("navig_email")

#: event → (notify type, priority, headline)
EVENTS: dict[str, tuple[str, str, str]] = {
    "draft": ("mail_outbox", "low", "Draft ready"),
    "pending_approval": ("mail_outbox", "normal", "Reply awaiting approval"),
    "scheduled": ("mail_outbox", "normal", "Reply queued"),
    "modified": ("mail_outbox", "low", "Queued reply changed"),
    "cancelled": ("mail_outbox", "low", "Queued reply cancelled"),
    "sent": ("mail_sent", "normal", "Reply sent"),
    "failed": ("mail_send_problem", "high", "Reply NOT sent"),
    "outcome_unknown": ("mail_send_problem", "high", "Reply may have been sent — checking"),
    "needs_review": ("mail_send_problem", "high", "Reply needs your review"),
    "auth_expired": ("mail_send_problem", "high", "Mailbox needs re-authorisation"),
}

#: Events a person caused themselves at a terminal don't need echoing back to their phone.
_QUIET_FOR_USER = {"draft", "pending_approval", "modified", "cancelled"}


def gmail_thread_url(account: str, thread_id: str) -> str:
    if not thread_id:
        return ""
    return f"https://mail.google.com/mail/?authuser={quote(account)}#all/{quote(thread_id)}"


def _mode(job: dict[str, Any]) -> str:
    if job.get("origin") == "policy":
        return f"Automatic reply ({job.get('policy_id') or 'policy'})"
    return "Scheduled send" if job.get("approved_at") else "Draft"


def render(event: str, job: dict[str, Any], note: str = "") -> tuple[str, str, str, str]:
    """``(type_key, priority, title, body)`` for one event. Pure — tested directly."""
    type_key, priority, head = EVENTS[event]
    lines = [
        f"To: {job.get('to_addrs') or '—'}",
        f"From: {job.get('from_addr') or job.get('account') or '—'}"
        + (f" · {job['identity']}" if job.get("identity") else ""),
        f"Subject: {job.get('subject') or '(no subject)'}",
        f"Mode: {_mode(job)}",
    ]
    if event in ("scheduled", "modified") and job.get("not_before"):
        lines.append(f"Sends at: {job['not_before'][:16].replace('T', ' ')} UTC (while this computer is online)")
    status = {
        "sent": "Confirmed sent",
        "failed": "Not sent",
        "outcome_unknown": "Unknown — searching Sent before anything is retried",
        "needs_review": "Waiting for you",
        "cancelled": "Cancelled — will not be sent",
        "scheduled": "Queued — not sent yet",
        "pending_approval": "Awaiting approval — not sent",
        "draft": "Draft — not sent",
        "modified": "Changed — not sent yet",
        "auth_expired": "Not sent — reconnect the mailbox",
    }.get(event, "")
    if status:
        lines.append(f"Status: {status}")
    if note:
        lines.append(f"Note: {note[:300]}")
    lines.append(f"Job: {str(job.get('job_id', ''))[:8]}")
    return type_key, priority, f"NAVIG Mailroom — {head}", "\n".join(lines)


def _keyboard(job: dict[str, Any], event: str = "") -> list[list[dict[str, str]]] | None:
    """State-aware action buttons (when this navig routes plugin buttons) + "Open thread"."""
    rows: list[list[dict[str, str]]] = []
    from navig_email.plugin import buttons_supported

    if job.get("job_id") and buttons_supported():
        try:
            from navig_email import tg

            rows.extend(tg.outbox_keyboard(job, event))
        except Exception:  # noqa: BLE001 — a token store problem drops the action buttons, not the message
            logger.debug("mailroom buttons unavailable", exc_info=True)
    url = gmail_thread_url(job.get("account", ""), job.get("thread_id", ""))
    if url:
        rows.append([{"text": "Open thread", "url": url}])
    return rows or None


async def deliver(event: str, job: dict[str, Any], note: str = "") -> bool:
    if event not in EVENTS:
        return False
    if event in _QUIET_FOR_USER and job.get("origin", "user") == "user":
        return False
    type_key, priority, title, body = render(event, job, note)
    key = f"mailroom:{event}:{job.get('job_id')}:{job.get('version')}"
    try:
        from navig.notify import dedupe
        from navig.notify.router import get_notification_router
    except ImportError:  # standalone: no router; the CLI already printed the outcome
        return False
    if not dedupe.claim(key, cooldown_s=10 * 365 * 86400):
        return False
    try:
        res = await get_notification_router().dispatch(
            type_key, title, body, priority=priority,
            data={"job_id": job.get("job_id"), "state": job.get("state"), "event": event},
            keyboard=_keyboard(job, event),
        )
    except Exception:  # noqa: BLE001
        logger.debug("mailroom notify dispatch failed", exc_info=True)
        dedupe.release(key)
        return False
    ok = any(c.get("ok") for c in (res or {}).get("channels") or [])
    if not ok:
        dedupe.release(key)
    return ok


def emit(event: str, job: dict[str, Any], note: str = "") -> None:
    """Fire-and-forget from sync code: inside the daemon's loop it is scheduled, from the CLI
    it runs to completion before returning."""
    coro = deliver(event, dict(job), note)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            asyncio.run(coro)
        except Exception:  # noqa: BLE001
            logger.debug("mailroom notify failed", exc_info=True)
        return
    try:
        from navig.core.background import spawn

        task = spawn(coro, name=f"mailroom-notify-{event}")
    except ImportError:
        task = asyncio.ensure_future(coro)
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)


_PENDING: set[asyncio.Future] = set()


async def drain(timeout: float = 15.0) -> None:
    """Wait for notifications started in this loop — a CLI's ``asyncio.run`` would otherwise
    cancel them on exit. The daemon never needs this: its loop outlives the task."""
    pending = [t for t in _PENDING if not t.done()]
    if pending:
        await asyncio.wait(pending, timeout=timeout)
