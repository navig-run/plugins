"""Mailroom 2.0 deck routes for NAVIG OS: outbox, automation, briefing, accounts, history.

Same rules as the rest of the mailroom API: every write takes an ACTION NAME from a fixed set,
approving needs the content hash the person was shown, "send now" needs ``confirm: true``, and
nothing here runs a command line. Bodies are returned only for one job at a time (``/outbox/job``),
never in lists.
"""

from __future__ import annotations

import json
import logging
from typing import Any

try:
    from aiohttp import web
except ImportError:  # pragma: no cover
    web = None

logger = logging.getLogger(__name__)

_LIST_FIELDS = ("job_id", "state", "origin", "policy_id", "provider", "account", "from_addr", "identity", "to_addrs",
                "subject", "thread_id", "not_before", "sent_at", "content_hash", "approved_by", "last_error",
                "created_at", "updated_at", "version")


def _ok(data: object) -> "web.Response":
    return web.json_response({"ok": True, "data": data})


def _err(msg: str, status: int = 400) -> "web.Response":
    return web.json_response({"ok": False, "error": msg}, status=status)


async def _body(request: "web.Request") -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001 — a bad body is an empty one
        return {}
    return data if isinstance(data, dict) else {}


def _job_public(job: dict[str, Any], *, body: bool = False) -> dict[str, Any]:
    out = {k: job.get(k) for k in _LIST_FIELDS}
    if job.get("quality_json"):
        try:
            out["quality"] = json.loads(job["quality_json"])
        except ValueError:
            pass
    if body:
        out["body"] = job.get("body")
        out["cc_addrs"] = job.get("cc_addrs")
    return out


# ── outbox ─────────────────────────────────────────────────────────────────


async def handle_outbox_list(request: "web.Request") -> "web.Response":
    from navig_email import outbox as ob

    states = [s.strip().upper() for s in (request.query.get("state") or "").split(",") if s.strip()]
    if any(s not in ob.STATES for s in states):
        return _err(f"state must be among {', '.join(ob.STATES)}")
    try:
        limit = max(1, min(200, int(request.query.get("limit") or 100)))
    except ValueError:
        return _err("limit must be an integer")
    jobs = ob.list_jobs(states=states or None, limit=limit)
    return _ok({"jobs": [_job_public(j) for j in jobs], "pause": ob.pause_level(),
                "counts": _counts(), "sends_while": "this computer is online"})


def _counts() -> dict[str, int]:
    from navig_email.store import get_store

    rows = get_store()._read_all("SELECT state, COUNT(*) AS n FROM outbox GROUP BY state")
    return {r["state"]: int(r["n"]) for r in rows}


async def handle_outbox_job(request: "web.Request") -> "web.Response":
    from navig_email import outbox as ob

    job = ob.get((request.query.get("id") or "").strip())
    if job is None:
        return _err("no such job", 404)
    return _ok({"job": _job_public(job, body=True), "events": ob.events(job["job_id"])})


OUTBOX_ACTIONS = ("approve", "cancel", "delay", "send_now", "reopen", "confirm_sent", "edit")


async def handle_outbox_act(request: "web.Request") -> "web.Response":
    from navig_email import mail_notify, outbox as ob

    b = await _body(request)
    action = str(b.get("action") or "")
    job_id = str(b.get("job") or "")
    if action not in OUTBOX_ACTIONS:
        return _err(f"action must be one of {', '.join(OUTBOX_ACTIONS)}")
    actor = "user:os"
    try:
        job = ob.require(job_id)
        if action == "approve":
            when = ob.parse_when(str(b["at"])) if b.get("at") else (
                ob.now_utc() + ob.parse_delay(str(b["in"])) if b.get("in") else None)
            job = ob.approve(job["job_id"], content_hash_seen=str(b.get("hash") or ""), send_at=when, actor=actor)
        elif action == "cancel":
            job = ob.cancel(job["job_id"], actor=actor)
        elif action == "delay":
            base = max(ob.parse_iso(job.get("not_before")) or ob.now_utc(), ob.now_utc())
            when = ob.parse_when(str(b["at"])) if b.get("at") else base + ob.parse_delay(str(b.get("by") or "1h"))
            job = ob.reschedule(job["job_id"], when, actor=actor)
        elif action == "send_now":
            if b.get("confirm") is not True:
                return _err("sending needs confirm: true", 400)
            job = ob.reschedule(job["job_id"], ob.now_utc(), actor=actor)
            await ob.tick(only=job["job_id"])
            await mail_notify.drain()
            job = ob.require(job["job_id"])
        elif action == "reopen":
            job = ob.reopen(job["job_id"], actor=actor)
        elif action == "confirm_sent":
            job = ob.mark_sent_manually(job["job_id"], actor=actor)
        elif action == "edit":
            job = ob.edit(job["job_id"], body=b.get("body"), subject=b.get("subject"), to=b.get("to"), actor=actor)
    except ob.Conflict as exc:
        return _err(str(exc), 409)
    except ob.OutboxError as exc:
        return _err(str(exc), 400)
    return _ok({"job": _job_public(job)})


async def handle_pause(request: "web.Request") -> "web.Response":
    from navig_email import outbox as ob

    if request.method == "GET":
        return _ok({"pause": ob.pause_level()})
    b = await _body(request)
    level = str(b.get("level") or "")
    try:
        ob.set_pause(level, actor="user:os")
    except ob.OutboxError as exc:
        return _err(str(exc))
    return _ok({"pause": ob.pause_level()})


# ── automation ─────────────────────────────────────────────────────────────


async def handle_policies(request: "web.Request") -> "web.Response":
    from navig_email import outbox as ob, policy as P

    return _ok({"policies": [{"enabled": p.enabled, **p.spec()} for p in P.all_policies()], "pause": ob.pause_level()})


async def handle_policy_act(request: "web.Request") -> "web.Response":
    from navig_email import policy as P

    b = await _body(request)
    action, pid = str(b.get("action") or ""), str(b.get("id") or "")
    try:
        if action == "disable":
            pol = P.disable(pid, actor="user:os")
        elif action == "enable":
            P.install_templates()
            pol = P.get(pid)
            if pol is None:
                return _err("no such policy", 404)
            if pol.action == "send" and b.get("confirm") is not True:
                return _err("this policy sends mail on its own: enabling it needs confirm: true")
            pol = P.enable(pid, account=str(b.get("account") or ""), aliases=b.get("aliases"),
                           senders=b.get("senders"), delay=b.get("delay"), actor="user:os")
        else:
            return _err("action must be enable or disable")
    except (P.PolicyError, ValueError) as exc:
        return _err(str(exc))
    return _ok({"policy": {"enabled": pol.enabled, **pol.spec()}})


# ── briefing ───────────────────────────────────────────────────────────────


async def handle_briefing(request: "web.Request") -> "web.Response":
    from navig_email.store import get_store

    s = get_store()
    status = (request.query.get("status") or "").strip()
    where, params = ("WHERE status = ?", (status,)) if status else ("", ())
    items = [dict(r) for r in s._read_all(f"SELECT * FROM digest_items {where} ORDER BY first_seen DESC LIMIT 100", params)]
    runs = [dict(r) for r in s._read_all("SELECT * FROM digest_runs ORDER BY started_at DESC LIMIT 20")]
    return _ok({
        "items": [{**{k: r[k] for k in ("item_id", "kind", "title", "status", "score", "first_seen")},
                   "data": json.loads(r["data_json"] or "{}")} for r in items],
        "runs": runs,
    })


async def handle_briefing_item(request: "web.Request") -> "web.Response":
    from navig_email.store import get_store

    b = await _body(request)
    status = str(b.get("status") or "")
    if status not in ("saved", "dismissed", "delivered"):
        return _err("status must be saved, dismissed or delivered")
    s = get_store()
    cur = s._write("UPDATE digest_items SET status = ? WHERE item_id = ?", (status, str(b.get("item") or "")))
    if not cur.rowcount:
        return _err("no such item", 404)
    s.audit(f"brief.{status}", actor="user:os", ref=str(b.get("item")))
    return _ok({"item": b.get("item"), "status": status})


# ── accounts & history ─────────────────────────────────────────────────────


async def handle_accounts(request: "web.Request") -> "web.Response":
    import asyncio

    from navig_email import account, gmail, senders
    from navig_email.overview import resolve_space
    from navig_email.store import get_store

    s = get_store()
    try:
        paths = resolve_space((request.query.get("space") or "").strip() or None)
    except ValueError:
        paths = None
    if request.method == "POST":
        b = await _body(request)
        from navig_email import outbox as ob

        acct = str(b.get("account") or "").strip().lower()
        if "@" not in acct:
            return _err("account must be an address")
        ob.disable_account(acct, bool(b.get("disabled")), actor="user:os")
    linked = await asyncio.to_thread(account.linked_accounts)
    rows = []
    for a in linked:
        rows.append({
            "account": a,
            "connected": await asyncio.to_thread(gmail.is_connected, a),
            "disabled": s.meta_get(f"account_disabled:{a.lower()}", "") == "1",
            "aliases": senders.aliases_for(paths, a) if paths is not None else [],
        })
    return _ok({"accounts": rows})


async def handle_history(request: "web.Request") -> "web.Response":
    from navig_email.store import get_store

    try:
        limit = max(1, min(500, int(request.query.get("limit") or 100)))
    except ValueError:
        return _err("limit must be an integer")
    return _ok({"audit": get_store().audit_rows(limit)})


async def handle_organize_audit(request: "web.Request") -> "web.Response":
    """Read-only: the label/filter audit and the proposed plan. Applying stays a CLI step."""
    from navig_email import account, organize as O
    from navig_email.overview import resolve_space

    try:
        paths = resolve_space((request.query.get("space") or "").strip() or None)
        c = await account.connector()
        a = await O.audit(c)
    except account.NotConnected as exc:
        return _err(str(exc), 409)
    except Exception as exc:  # noqa: BLE001
        logger.exception("organize audit failed")
        return _err(str(exc), 500)
    return _ok({"audit": a.as_dict(), "plan": O.preview(a, paths=paths).as_dict()})


def register(app: "web.Application", gate) -> None:
    base = "/api/deck/email/mailroom"
    app.router.add_get(f"{base}/outbox", gate(handle_outbox_list))
    app.router.add_get(f"{base}/outbox/job", gate(handle_outbox_job))
    app.router.add_post(f"{base}/outbox/act", gate(handle_outbox_act))
    app.router.add_get(f"{base}/pause", gate(handle_pause))
    app.router.add_post(f"{base}/pause", gate(handle_pause))
    app.router.add_get(f"{base}/policies", gate(handle_policies))
    app.router.add_post(f"{base}/policies/act", gate(handle_policy_act))
    app.router.add_get(f"{base}/briefing", gate(handle_briefing))
    app.router.add_post(f"{base}/briefing/item", gate(handle_briefing_item))
    app.router.add_get(f"{base}/accounts", gate(handle_accounts))
    app.router.add_post(f"{base}/accounts", gate(handle_accounts))
    app.router.add_get(f"{base}/history", gate(handle_history))
    app.router.add_get(f"{base}/organize/audit", gate(handle_organize_audit))
