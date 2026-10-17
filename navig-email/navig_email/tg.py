"""Mailroom buttons on Telegram: ``mr:<opaque token>`` → an action kept server-side.

A button carries only a random 12-character token. What it does, on which job or item, which
text version it approves, and until when it is valid live in ``mailroom.db`` (``cb_tokens``) —
never in the message, never in a URL. So:

* a forwarded or copied button is useless outside the operator's authorised chat (core checks
  the user and chat before any plugin sees a press, and a token bound to a chat refuses others);
* a token expires (24 h for anything that sends, 14 days for save / dismiss);
* sending, approving and cancelling tokens are **single-use** — a second press answers
  "already done" instead of acting twice; "Send now" asks for a confirming second tap;
* an approval token carries the content hash shown in that message: if the text was edited
  since, the approval is refused (the outbox's own rule).
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from navig_email import outbox as ob
from navig_email.store import MailroomStore, get_store

logger = logging.getLogger("navig_email")

PREFIX = "mr:"
SEND_TTL = timedelta(hours=24)
ITEM_TTL = timedelta(days=14)
#: Actions that change what is sent: single-use, short-lived.
SINGLE_USE = {"ob.cancel", "ob.send_now_confirm", "ob.approve", "ob.delay"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def make(action: str, args: dict[str, Any], *, chat_id: str = "", ttl: timedelta | None = None,
         store: MailroomStore | None = None) -> str:
    """A ``callback_data`` string for *action* (≤ 64 bytes, as Telegram requires)."""
    s = store or get_store()
    token = secrets.token_urlsafe(9)  # 12 chars
    life = ttl or (SEND_TTL if action.startswith("ob.") else ITEM_TTL)
    s._write(
        "INSERT INTO cb_tokens (token, action, args_json, chat_id, single_use, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
        (token, action, json.dumps(args, sort_keys=True), str(chat_id or ""), int(action in SINGLE_USE),
         _iso(_now() + life)),
    )
    return PREFIX + token


def button(text: str, action: str, args: dict[str, Any], **kw: Any) -> dict[str, str]:
    return {"text": text, "callback_data": make(action, args, **kw)}


def purge_expired(store: MailroomStore | None = None) -> int:
    s = store or get_store()
    cur = s._write("DELETE FROM cb_tokens WHERE expires_at < ?", (_iso(_now() - timedelta(days=1)),))
    return cur.rowcount


# ── keyboards ──────────────────────────────────────────────────────────────


def outbox_keyboard(job: dict[str, Any], event: str) -> list[list[dict[str, str]]]:
    """Only the buttons that make sense for the job's current state."""
    jid = job["job_id"]
    rows: list[list[dict[str, str]]] = []
    state = job.get("state")
    if state == ob.SCHEDULED:
        rows.append([button("View", "ob.view", {"job": jid}), button("Send now", "ob.send_now", {"job": jid})])
        rows.append([button("Delay 1 h", "ob.delay", {"job": jid, "by": "1h"}),
                     button("Cancel", "ob.cancel", {"job": jid})])
    elif state == ob.PENDING_APPROVAL:
        rows.append([button("View", "ob.view", {"job": jid}),
                     button("Approve · send in 15 min", "ob.approve", {"job": jid, "hash": job["content_hash"]})])
        rows.append([button("Cancel", "ob.cancel", {"job": jid})])
    elif state in (ob.NEEDS_REVIEW, ob.FAILED, ob.OUTCOME_UNKNOWN, ob.SENT, ob.DRAFT):
        rows.append([button("View", "ob.view", {"job": jid})])
    if ob.pause_level() == "" and job.get("origin") == "policy" and state == ob.SCHEDULED:
        rows.append([button("Pause automatic replies", "ob.pause", {})])
    return rows


def item_buttons(item_id: str, kind: str) -> list[dict[str, str]]:
    """Save / Dismiss for one briefing item (the link buttons come from the briefing renderer)."""
    return [button("Save", "bi.save", {"item": item_id}), button("Dismiss", "bi.dismiss", {"item": item_id})]


# ── handling a press ───────────────────────────────────────────────────────


def _resolve(token: str, store: MailroomStore) -> dict[str, Any] | None:
    row = store._read_one("SELECT * FROM cb_tokens WHERE token = ?", (token,))
    return dict(row) if row is not None else None


def _claim(token: str, store: MailroomStore) -> bool:
    """Atomically mark a single-use token used. False when someone (a double tap) got there first."""
    cur = store._write("UPDATE cb_tokens SET used_at = ? WHERE token = ? AND used_at IS NULL", (_iso(_now()), token))
    return cur.rowcount == 1


async def handle(channel: Any, callback_query: dict[str, Any], data: str, *, store: MailroomStore | None = None) -> None:
    """The ``mr:`` handler registered with core (navig.telegram.plugin_callbacks)."""
    from navig.telegram.plugin_callbacks import answer

    s = store or get_store()
    token = data[len(PREFIX):]
    row = _resolve(token, s)
    chat = str(((callback_query.get("message") or {}).get("chat") or {}).get("id") or "")
    if row is None:
        await answer(channel, callback_query, "This button is no longer valid.", alert=True)
        return
    if row["expires_at"] < _iso(_now()):
        await answer(channel, callback_query, "This button expired — open the mailroom in NAVIG OS.", alert=True)
        return
    if row["chat_id"] and row["chat_id"] != chat:
        await answer(channel, callback_query, "This button belongs to another chat.", alert=True)
        return
    if row["single_use"]:
        if row["used_at"] or not _claim(token, s):
            await answer(channel, callback_query, f"Already done{': ' + row['result'] if row['result'] else ''}.")
            return
    args = json.loads(row["args_json"] or "{}")
    try:
        result = await _act(channel, callback_query, row["action"], args, chat=chat, store=s)
    except ob.OutboxError as exc:
        result = f"Not done: {exc}"
    s._write("UPDATE cb_tokens SET result = ? WHERE token = ?", (result[:200], token))
    s.audit(f"telegram.{row['action']}", actor="telegram", ref=str(args.get("job") or args.get("item") or ""),
            summary=result[:200])
    await answer(channel, callback_query, result)


async def _send(channel: Any, chat: str, text: str, keyboard: list[list[dict[str, str]]] | None = None) -> None:
    payload: dict[str, Any] = {"chat_id": chat, "text": text[:4000], "disable_web_page_preview": True}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    await channel._api_call("sendMessage", payload)


async def _act(channel: Any, cq: dict[str, Any], action: str, args: dict[str, Any], *, chat: str,
               store: MailroomStore) -> str:
    if action == "ob.view":
        job = ob.require(args["job"], store)
        text = (f"To: {job['to_addrs']}\nFrom: {job['from_addr']}\nSubject: {job['subject']}\n"
                f"State: {job['state'].lower().replace('_', ' ')}\n\n{job['body']}")
        await _send(channel, chat, text, outbox_keyboard(job, "view") if job["state"] in (ob.SCHEDULED, ob.PENDING_APPROVAL) else None)
        return "Shown below."
    if action == "ob.send_now":
        job = ob.require(args["job"], store)
        if job["state"] != ob.SCHEDULED:
            return f"It is {job['state'].lower().replace('_', ' ')} — nothing to send now."
        confirm = [[button("Yes, send it now", "ob.send_now_confirm", {"job": job["job_id"]}, chat_id=chat),
                    button("No", "ob.view", {"job": job["job_id"]}, chat_id=chat)]]
        await _send(channel, chat, f"Send “{job['subject']}” to {job['to_addrs']} now?", confirm)
        return "Confirm below."
    if action == "ob.send_now_confirm":
        job = ob.reschedule(args["job"], ob.now_utc(), actor="telegram", store=store)
        stats = await ob.tick(only=job["job_id"], store=store)
        final = ob.require(job["job_id"], store)
        return {ob.SENT: "Sent (confirmed by the provider).", ob.OUTCOME_UNKNOWN: "Sent? Unknown — checking Sent first.",
                ob.FAILED: "Not sent: " + final.get("last_error", ""),
                ob.NEEDS_REVIEW: "Not sent — needs a look: " + final.get("last_error", "")}.get(
            final["state"], f"{final['state'].lower()} ({stats})")
    if action == "ob.cancel":
        ob.cancel(args["job"], actor="telegram", store=store)
        return "Cancelled — it will not be sent."
    if action == "ob.delay":
        job = ob.require(args["job"], store)
        base = max(ob.parse_iso(job.get("not_before")) or ob.now_utc(), ob.now_utc())
        job = ob.reschedule(job["job_id"], base + ob.parse_delay(args.get("by") or "1h"), actor="telegram", store=store)
        return f"Moved to {job['not_before'][11:16]} UTC."
    if action == "ob.approve":
        job = ob.approve(args["job"], content_hash_seen=args.get("hash", ""), actor="telegram", store=store)
        return f"Approved — sends at {job['not_before'][11:16]} UTC unless cancelled."
    if action == "ob.pause":
        ob.set_pause("auto", actor="telegram", store=store)
        return "Automatic replies paused. Queued ones stay cancellable. Resume from NAVIG."
    if action in ("bi.save", "bi.dismiss"):
        status = "saved" if action == "bi.save" else "dismissed"
        cur = store._write("UPDATE digest_items SET status = ? WHERE item_id = ?", (status, args["item"]))
        if not cur.rowcount:
            return "That item is gone."
        return "Saved — its email will be kept." if status == "saved" else "Dismissed."
    return "Unknown action."
