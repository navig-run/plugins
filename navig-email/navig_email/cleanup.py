"""What happens to a newsletter after its content reached you — archive by default, Trash only
when you asked for it, permanent deletion never.

Order of prerequisites (each one is checked, none is assumed):

1. the briefing that used the message was **delivered** (a verified Telegram send — "built" or
   "attempted" is not enough; a failed delivery leaves every source untouched);
2. the message gave at least one item, and every one of its items was in a delivered briefing;
3. none of its items is saved / pending an action;
4. the message is machine-sent newsletter mail with no attachment and no protected content
   (contracts, invoices, government, finance, security, account recovery, job applications —
   :func:`navig_email.safety.hard_exclusions`);

then it gets the ``Mailroom/Digested`` label and leaves the inbox (still in All Mail). Only if
its sender is in ``cleanup.trash_senders`` does it also get a ``trash_after`` date; a later pass
moves it to Gmail's Trash (recoverable for 30 days; ``restore`` brings it back). A delivered
briefing is not a read briefing — nothing here marks anything as read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from typing import Any

from navig_email import safety
from navig_email.store import MailroomStore, get_store

DIGESTED_LABEL = "Mailroom/Digested"
PROTECTED = {"contract", "legal", "payment", "bank", "financial", "employment", "government",
             "account_recovery", "credentials", "security_incident", "sensitive_attachment", "irreversible"}
DEFAULT_RETENTION_DAYS = 7


@dataclass
class CleanupConfig:
    archive: bool = True
    trash_senders: list[str] = field(default_factory=list)
    retention_days: int = DEFAULT_RETENTION_DAYS

    @classmethod
    def from_config(cls, cfg: dict[str, Any] | None) -> CleanupConfig:
        c = dict(cfg or {})
        return cls(archive=bool(c.get("archive", True)),
                   trash_senders=[str(s).strip().lower().lstrip("@") for s in c.get("trash_senders") or []],
                   retention_days=int(c.get("trash_after_days") or DEFAULT_RETENTION_DAYS))


@dataclass
class CleanupResult:
    archived: list[str] = field(default_factory=list)
    scheduled_trash: list[str] = field(default_factory=list)
    trashed: list[str] = field(default_factory=list)
    kept: dict[str, str] = field(default_factory=dict)  # message id → why it was left alone
    errors: list[str] = field(default_factory=list)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def why_not(msg: dict[str, Any], *, account: str, store: MailroomStore) -> str:
    """Why *msg* must stay in the inbox (``""`` when it may be archived)."""
    mid = str(msg.get("id") or "")
    life = store._read_one("SELECT state, note FROM lifecycle WHERE account = ? AND message_id = ?", (account, mid))
    if life is not None and life["state"] == "kept":
        return f"kept: {life['note'] or 'marked to stay'}"
    rows = store._read_all(
        "SELECT di.status FROM digest_sources ds JOIN digest_items di ON di.item_id = ds.item_id "
        "WHERE ds.account = ? AND ds.message_id = ?", (account, mid))
    if not rows:
        return "no item was extracted from it"
    if any(r["status"] == "saved" for r in rows):
        return "an item from it is saved / pending"
    if any(r["status"] != "delivered" and r["status"] != "dismissed" for r in rows):
        return "not every item from it was delivered"
    if msg.get("has_attachment"):
        return "it has an attachment"
    if not safety.automated(msg):
        return "it does not look like newsletter mail"
    hit = PROTECTED & set(safety.hard_exclusions(msg).categories)
    if hit:
        return "protected content: " + ", ".join(sorted(hit))
    return ""


async def _label_id(connector, name: str) -> str:
    return await connector.ensure_label(name)


async def _trash(connector, mid: str):
    act = getattr(connector, "act", None)
    if callable(act):
        from navig_email.errors import Action, ActionType

        return await act(Action(action_type=ActionType.DELETE, resource_id=mid))
    return await connector.modify_message(mid, add=["TRASH"], remove=["INBOX"])


async def after_delivery(
    connector, msgs: list[dict[str, Any]], *, account: str, run_id: str, cfg: CleanupConfig,
    store: MailroomStore | None = None, now: datetime | None = None, dry_run: bool = False,
) -> CleanupResult:
    """Archive (and maybe schedule for Trash) the sources of a DELIVERED briefing."""
    s = store or get_store()
    t = now or _now()
    res = CleanupResult()
    run = s._read_one("SELECT delivery_state FROM digest_runs WHERE run_id = ?", (run_id,))
    if run is None or run["delivery_state"] != "delivered":
        res.errors.append(f"briefing {run_id[:8]} was not delivered — nothing is cleaned up")
        return res
    if not cfg.archive:
        return res
    label = None
    for msg in msgs:
        mid = str(msg.get("id") or "")
        reason = why_not(msg, account=account, store=s)
        if reason:
            res.kept[mid] = reason
            continue
        if dry_run:
            res.archived.append(mid)
            continue
        try:
            label = label or await _label_id(connector, DIGESTED_LABEL)
            out = await connector.modify_message(mid, add=[label], remove=["INBOX"])
            if not getattr(out, "success", True):
                res.errors.append(f"{mid}: {getattr(out, 'error', 'archive failed')}")
                continue
        except Exception as exc:  # noqa: BLE001 — one message's failure is reported, the rest continue
            res.errors.append(f"{mid}: {exc}")
            continue
        res.archived.append(mid)
        domain = parseaddr(str(msg.get("from") or ""))[1].rpartition("@")[2].lower()
        trash_after = None
        if any(domain == d or domain.endswith("." + d) for d in cfg.trash_senders):
            trash_after = _iso(t + timedelta(days=cfg.retention_days))
            res.scheduled_trash.append(mid)
        s._write(
            "INSERT INTO lifecycle (account, message_id, state, run_id, trash_after, updated_at, note) "
            "VALUES (?, ?, 'archived', ?, ?, ?, '') ON CONFLICT(account, message_id) DO UPDATE SET "
            "state='archived', run_id=excluded.run_id, trash_after=excluded.trash_after, updated_at=excluded.updated_at",
            (account, mid, run_id, trash_after, _iso(t)),
        )
        s.audit("cleanup.archive", actor="brief", account=account, ref=mid, summary=f"run {run_id[:8]}"
                + (f" · trash after {trash_after[:10]}" if trash_after else ""))
    return res


async def trash_due(connector, *, account: str, store: MailroomStore | None = None, now: datetime | None = None,
                    dry_run: bool = False) -> CleanupResult:
    """Move archived newsletters whose retention ran out to Gmail's Trash (recoverable)."""
    s = store or get_store()
    t = now or _now()
    res = CleanupResult()
    rows = s._read_all("SELECT * FROM lifecycle WHERE account = ? AND state = 'archived' AND trash_after IS NOT NULL "
                       "AND trash_after <= ?", (account, _iso(t)))
    for row in rows:
        mid = row["message_id"]
        pending = s._read_one(
            "SELECT 1 FROM digest_sources ds JOIN digest_items di ON di.item_id = ds.item_id "
            "WHERE ds.account = ? AND ds.message_id = ? AND di.status = 'saved' LIMIT 1", (account, mid))
        if pending is not None:
            res.kept[mid] = "an item from it is saved / pending"
            continue
        if dry_run:
            res.trashed.append(mid)
            continue
        try:
            out = await _trash(connector, mid)
        except Exception as exc:  # noqa: BLE001
            res.errors.append(f"{mid}: {exc}")
            continue
        if not getattr(out, "success", True):
            res.errors.append(f"{mid}: {getattr(out, 'error', 'trash failed')}")
            continue
        s._write("UPDATE lifecycle SET state='trashed', updated_at=? WHERE account=? AND message_id=?",
                 (_iso(t), account, mid))
        s.audit("cleanup.trash", actor="brief", account=account, ref=mid, summary="moved to Trash (recoverable)")
        res.trashed.append(mid)
    return res


async def restore(connector, *, account: str, message_id: str, store: MailroomStore | None = None) -> bool:
    """Back to the inbox: out of Trash if it went there, digested label removed."""
    s = store or get_store()
    row = s._read_one("SELECT state FROM lifecycle WHERE account=? AND message_id=?", (account, message_id))
    if row is None:
        return False
    if row["state"] == "trashed":
        post = getattr(connector, "_api_post", None)
        if callable(post):  # the Gmail API: Trash is left through untrash, not a label edit
            await post(f"/users/me/messages/{message_id}/untrash")
        else:
            await connector.modify_message(message_id, remove=["TRASH"])
    label = await _label_id(connector, DIGESTED_LABEL)
    await connector.modify_message(message_id, add=["INBOX"], remove=[label])
    s._write("UPDATE lifecycle SET state='restored', trash_after=NULL, updated_at=? WHERE account=? AND message_id=?",
             (_iso(_now()), account, message_id))
    s.audit("cleanup.restore", actor="user", account=account, ref=message_id)
    return True
