"""One briefing run: pick sources → extract → synthesize → (postpone | deliver) → record → clean up.

Every state is recorded in ``mailroom.db``: the run (``digest_runs``), each item and which
emails it came from (``digest_items`` / ``digest_sources``), and what happened to each source
(``lifecycle``). Items stay ``new`` until a delivery is **verified**; a failed delivery leaves
them to the next run and leaves every source email exactly where it was.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from navig_email import cleanup as CL, messages, safety
from navig_email.brief import extract as X, render as R, schedule as SCH, synthesize as SY
from navig_email.brief.model import Item
from navig_email.store import MailroomStore, dumps, get_store

logger = logging.getLogger("navig_email")

DEFAULT_QUERY = "newer_than:14d -in:sent -in:spam -in:trash -in:drafts -label:Mailroom/Digested"
SCAN_LIMIT = 60

#: ``(messages) -> (delivered, reference, detail)``. Default: navig's notify router, type mail_digest.
Deliver = Callable[[list[tuple[str, str, list[list[dict[str, str]]]]]], Awaitable[tuple[bool, str, str]]]


@dataclass
class BriefResult:
    status: str  # delivered | postponed | not_due | empty | failed | dry_run | built
    reason: str = ""
    run_id: str = ""
    sources: list[dict[str, Any]] = field(default_factory=list)
    sections: dict[str, list[Item]] = field(default_factory=dict)
    rendered: list[tuple[str, str, list[list[dict[str, str]]]]] = field(default_factory=list)
    cleanup: CL.CleanupResult | None = None
    alerts: list[str] = field(default_factory=list)

    @property
    def item_count(self) -> int:
        return SY.count(self.sections)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")) if text else None
    except ValueError:
        return None


async def default_deliver(msgs: list[tuple[str, str, list[list[dict[str, str]]]]]) -> tuple[bool, str, str]:
    """Telegram through the notify router. Delivered = every message reached Telegram (or, when
    Telegram is off for mail digests, the deck feed). Anything less is a failed delivery."""
    try:
        from navig.notify.router import get_notification_router
    except ImportError:
        return False, "", "navig's notify router is not available (standalone)"
    router = get_notification_router()
    refs, details = [], []
    for title, text, rows in msgs:
        res = await router.dispatch("mail_digest", title, text, priority="normal", keyboard=rows or None,
                                    data={"kind": "mailroom_brief"})
        chans = {c.get("channel"): c for c in (res or {}).get("channels") or []}
        tg = chans.get("telegram")
        ok = bool(tg and tg.get("ok")) if tg is not None else any(c.get("ok") for c in chans.values())
        if not ok:
            details.append(f"{title}: " + ", ".join(f"{k}={v.get('detail') or v.get('ok')}" for k, v in chans.items())
                           or "no channel accepted it")
            return False, "", "; ".join(details)
        refs.append(",".join(sorted(k for k, v in chans.items() if v.get("ok"))))
    return True, "|".join(refs), ""


def _meta_key(account: str, name: str) -> str:
    return f"brief:{account}:{name}"


#: Senders whose newsletters a deterministic parser understands — preferred as sources.
KNOWN_SENDERS = ("producthunt.com", "free-work.com", "github.com", "meetup.com", "eventbrite.com", "lu.ma",
                 "welcometothejungle.com", "malt.fr")
#: Newsletters that mail from a third-party domain, recognised by their display name.
KNOWN_NAMES = ("product hunt",)
#: Never briefing material, whatever headers it carries.
_NEVER = {"account_recovery", "credentials", "security_incident", "payment", "bank", "government", "legal",
          "contract", "employment", "irreversible"}


def _domain(msg: dict[str, Any]) -> str:
    return messages.domain_of(str(msg.get("from") or ""))


def _known_sender(msg: dict[str, Any]) -> bool:
    d = _domain(msg)
    name = messages.display_name(str(msg.get("from") or "")).lower()
    return any(d == k or d.endswith("." + k) for k in KNOWN_SENDERS) or any(n in name for n in KNOWN_NAMES)


def is_source(msg: dict[str, Any], *, allowed: list[str] | None = None) -> bool:
    """A newsletter the briefing may read: bulk mail with a list header, from an allowed sender
    (when ``brief.sources`` names any), touching nothing protected. Transactional mail — password
    resets, receipts, account notices — has no list header and is never read."""
    labels = set(msg.get("labels") or [])
    if labels & {"SPAM", "TRASH", "SENT", "DRAFT"}:
        return False
    h = safety.headers_of(msg)
    if not (h.get("list-unsubscribe") or h.get("list-id")):
        return False
    d = _domain(msg)
    if allowed and not any(d == a or d.endswith("." + a) for a in allowed):
        return False
    if _NEVER & set(safety.hard_exclusions({"subject": msg.get("subject"), "body": msg.get("snippet")}).categories):
        return False
    return True


async def candidates(connector, *, account: str, cfg: SCH.BriefConfig, store: MailroomStore,
                     query: str | None = None) -> list[dict[str, Any]]:
    """The most recent newsletter-like messages not yet used, up to ``max_sources``, with bodies."""
    q = query or str((cfg.raw or {}).get("query") or DEFAULT_QUERY)
    entries = await connector.iter_message_ids(q, limit=SCAN_LIMIT)
    if not entries:
        return []
    meta = await connector.get_many([e["id"] for e in entries], format="metadata")
    allowed = [str(d).strip().lower().lstrip("@") for d in (cfg.raw or {}).get("sources") or []]
    picked = []
    for m in meta:
        msg = messages.shape(m)
        if not is_source(msg, allowed=allowed):
            continue
        done = store._read_one("SELECT state FROM lifecycle WHERE account=? AND message_id=?", (account, msg["id"]))
        if done is not None and done["state"] in ("processed", "delivered", "archived", "trashed", "restored"):
            continue
        picked.append(msg)
    # Senders a parser understands first (they yield real items), then the most recent.
    picked.sort(key=lambda m: (0 if _known_sender(m) else 1, -int(m.get("internal_ms") or 0)))
    picked = picked[: cfg.max_sources]
    full = await connector.get_many([m["id"] for m in picked], format="full")
    return [messages.shape(m, body=connector._extract_body(m.get("payload") or {})) for m in full]


def _record_items(store: MailroomStore, run_id: str, items: list[Item], now: datetime) -> None:
    with store.transaction() as conn:
        for it in items:
            fp = it.fingerprint()
            conn.execute(
                "INSERT INTO digest_items (item_id, fingerprint, kind, title, data_json, score, first_seen, run_id, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new') ON CONFLICT(item_id) DO UPDATE SET data_json=excluded.data_json, "
                "score=excluded.score, run_id=excluded.run_id",
                (fp, fp, it.kind, it.title[:300], dumps(it.as_dict()), it.score, _iso(now), run_id),
            )
            for src in it.sources:
                conn.execute("INSERT OR IGNORE INTO digest_sources (item_id, account, message_id) VALUES (?, ?, ?)",
                             (fp, src.account, src.message_id))


def _mark_delivered(store: MailroomStore, run_id: str, items: list[Item], sources: list[dict[str, Any]], account: str,
                    ref: str, now: datetime) -> None:
    with store.transaction() as conn:
        for it in items:
            conn.execute("UPDATE digest_items SET status='delivered', run_id=? WHERE item_id=? AND status IN ('new', 'delivered')",
                         (run_id, it.fingerprint()))
        used = {s.message_id for it in items for s in it.sources}
        for msg in sources:
            if msg["id"] in used:
                conn.execute(
                    "INSERT INTO lifecycle (account, message_id, state, run_id, updated_at) VALUES (?, ?, 'delivered', ?, ?) "
                    "ON CONFLICT(account, message_id) DO UPDATE SET state='delivered', run_id=excluded.run_id, "
                    "updated_at=excluded.updated_at WHERE lifecycle.state IN ('seen', 'processed', 'delivered')",
                    (account, msg["id"], run_id, _iso(now)),
                )
        conn.execute("UPDATE digest_runs SET delivery_state='delivered', delivery_ref=?, finished_at=? WHERE run_id=?",
                     (ref[:500], _iso(now), run_id))


async def run(
    paths: Any,
    *,
    connector: Any,
    account: str,
    now: datetime | None = None,
    scheduled: bool = False,
    send: bool = True,
    dry_run: bool = False,
    deliver: Deliver | None = None,
    store: MailroomStore | None = None,
    query: str | None = None,
    use_model: bool | None = None,
    model: str | None = None,
    allow_cloud: bool = False,
) -> BriefResult:
    s = store or get_store()
    t = now or datetime.now(timezone.utc)
    brief_cfg = ((paths.config.get("mailroom") or {}).get("brief") if paths is not None else None) or {}
    cfg = SCH.BriefConfig.from_config(brief_cfg)
    interests = SY.Interests.from_config(brief_cfg)
    last_delivered = _parse(s.meta_get(_meta_key(account, "last_delivered")))
    last_slot_served = _parse(s.meta_get(_meta_key(account, "last_slot")))

    slot = None
    if scheduled and cfg.frequency != "adaptive":
        d = SCH.due(t, cfg, last_delivered=last_delivered, last_slot_served=last_slot_served)
        if not d.due:
            res = BriefResult("not_due", d.reason)
            res.alerts = await deadline_alerts(account=account, cfg=cfg, now=t, store=s, deliver=deliver,
                                               dry_run=dry_run or not send)
            return res
        slot = d.slot
    elif scheduled and SCH.in_quiet_hours(t, cfg):
        return BriefResult("not_due", "quiet hours")

    srcs = await candidates(connector, account=account, cfg=cfg, store=s, query=query)
    want_model = bool(paths is not None and paths.ai_model) if use_model is None else use_model
    items: list[Item] = []
    for msg in srcs:
        got = X.extract(msg, account=account, store=s, use_model=want_model,
                        model=model or (paths.ai_model if paths is not None else None), allow_cloud=allow_cloud)
        items.extend(got)
        if not got and not dry_run and X.is_cached(msg, store=s):
            # Read, understood, nothing useful: don't let it take a source slot on every run.
            s._write("INSERT OR IGNORE INTO lifecycle (account, message_id, state, updated_at, note) "
                     "VALUES (?, ?, 'processed', ?, 'no items')", (account, msg["id"], _iso(t)))
    sections = SY.synthesize(items, interests=interests, today=t.astimezone(cfg.tz).date(), store=s,
                             caps=(brief_cfg.get("caps") if isinstance(brief_cfg.get("caps"), dict) else None))
    chosen = [i for v in sections.values() for i in v]
    res = BriefResult("built", sources=srcs, sections=sections)
    brief_title = f"NAVIG Mailroom — Briefing {t.astimezone(cfg.tz):%a %d %b}"
    res.rendered = R.messages(sections, title=brief_title)

    if scheduled:
        if cfg.frequency == "adaptive":
            if not SCH.adaptive_due(len(chosen), t, cfg, last_delivered):
                res.status, res.reason = "postponed", f"{len(chosen)} item(s) — waiting for {cfg.min_items}"
                return res
        elif SCH.should_postpone(len(chosen), t, cfg, last_delivered):
            res.status, res.reason = "postponed", (
                f"{len(chosen)} item(s) < {cfg.min_items}; postponed (at most {cfg.max_postpone_days} days)")
            if not dry_run:
                s.meta_set(_meta_key(account, "postponed_since"),
                           s.meta_get(_meta_key(account, "postponed_since")) or _iso(t))
            return res
    if not chosen:
        res.status, res.reason = "empty", "nothing new worth a briefing"
        if scheduled and slot is not None and not dry_run:
            s.meta_set(_meta_key(account, "last_slot"), _iso(slot))
        return res
    if dry_run or not send:
        res.status = "dry_run" if dry_run else "built"
        return res

    run_id = uuid.uuid4().hex
    res.run_id = run_id
    s._write("INSERT INTO digest_runs (run_id, category, started_at, item_count, delivery_state) VALUES (?, 'all', ?, ?, 'pending')",
             (run_id, _iso(t), len(chosen)))
    _record_items(s, run_id, chosen, t)
    from navig_email.plugin import buttons_supported

    if buttons_supported():
        res.rendered = R.messages(sections, title=brief_title, actions=True)
    ok, ref, detail = await (deliver or default_deliver)(res.rendered)
    if not ok:
        s._write("UPDATE digest_runs SET delivery_state='failed', note=?, finished_at=? WHERE run_id=?",
                 (detail[:500], _iso(t), run_id))
        s.audit("brief.failed", actor="brief", account=account, ref=run_id, summary=detail[:300])
        res.status, res.reason = "failed", detail or "delivery failed"
        return res
    _mark_delivered(s, run_id, chosen, srcs, account, ref, t)
    s.meta_set(_meta_key(account, "last_delivered"), _iso(t))
    s.meta_set(_meta_key(account, "postponed_since"), "")
    if slot is not None:
        s.meta_set(_meta_key(account, "last_slot"), _iso(slot))
    s.audit("brief.delivered", actor="brief", account=account, ref=run_id,
            summary=f"{len(chosen)} items from {len({x.message_id for i in chosen for x in i.sources})} emails")
    res.status = "delivered"
    ccfg = CL.CleanupConfig.from_config(brief_cfg.get("cleanup") if isinstance(brief_cfg.get("cleanup"), dict) else None)
    res.cleanup = await CL.after_delivery(connector, srcs, account=account, run_id=run_id, cfg=ccfg, store=s, now=t)
    trashed = await CL.trash_due(connector, account=account, store=s, now=t)
    res.cleanup.trashed.extend(trashed.trashed)
    res.cleanup.errors.extend(trashed.errors)
    res.alerts = await deadline_alerts(account=account, cfg=cfg, now=t, store=s, deliver=deliver)
    return res


async def deadline_alerts(*, account: str, cfg: SCH.BriefConfig, now: datetime, store: MailroomStore,
                          deliver: Deliver | None = None, dry_run: bool = False) -> list[str]:
    """Events (or registration deadlines) within ``alert_days`` are announced on their own, once —
    a weekly briefing must not sit on a deadline that falls before it."""
    import json

    if cfg.alert_days <= 0 or SCH.in_quiet_hours(now, cfg):
        return []
    today = now.astimezone(cfg.tz).date()
    horizon = today + timedelta(days=cfg.alert_days)
    out: list[str] = []
    for row in store._read_all("SELECT item_id, data_json FROM digest_items WHERE kind='event' AND status != 'dismissed'"):
        it = Item.from_dict(json.loads(row["data_json"]))
        key = it.fields.get("deadline") or it.fields.get("date")
        try:
            when = datetime.fromisoformat(str(key)).date()
        except ValueError:
            continue
        if not (today <= when <= horizon):
            continue
        mark = _meta_key(account, f"alert:{row['item_id']}:{when.isoformat()}")
        if store.meta_get(mark):
            continue
        what = "Registration closes" if it.fields.get("deadline") else "Coming up"
        msgs = [(f"NAVIG Mailroom — {what}: {it.title}", R.item_text(it), [R.buttons(it)] if R.buttons(it) else [])]
        if dry_run:
            out.append(it.title)
            continue
        ok, _ref, _detail = await (deliver or default_deliver)(msgs)
        if ok:
            store.meta_set(mark, _iso(now))
            out.append(it.title)
    return out
