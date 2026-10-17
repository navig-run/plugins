"""The outbox — every message the mailroom sends, as one auditable state machine.

::

    DRAFT ─▶ PENDING_APPROVAL ─▶ SCHEDULED ─▶ CLAIMED ─▶ SENDING ─▶ SENT
                  ▲    │              │  ▲        │          ├──▶ FAILED
                  └────┴── (edit) ────┘  └(release)┘          └──▶ OUTCOME_UNKNOWN ─▶ SENT
                                                                                    └─▶ NEEDS_REVIEW
    any open state ─▶ CANCELLED

Rules this module enforces (and the tests prove):

* **One row per message.** ``op_key`` is UNIQUE; enqueueing the same intent twice returns the
  first job.
* **Approval binds content.** ``approve`` records the hash of the exact text the approver saw;
  any edit resets the job to ``PENDING_APPROVAL``; dispatch refuses when the hashes differ.
* **Every transition is conditional** (``WHERE state=? AND version=?``). Cancel and claim race
  on the same row and exactly one wins; a cancelled job can never be claimed.
* **Never a blind resend.** A provider timeout lands in ``OUTCOME_UNKNOWN``; only finding the
  job's own ``Message-ID`` in Sent turns it into ``SENT``. Not finding it after the window
  turns it into ``NEEDS_REVIEW`` — a person decides, the dispatcher never does.
* **Revalidate at dispatch**: pause, account still connected, sender still authorised,
  approval still matching, policy still enabled, not too stale.
* **"Sent" is said once, from the SENT transition only.** Creating a job is not sending it.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import socket
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses
from typing import Any

from navig_email import providers
from navig_email.store import MailroomStore, get_store

logger = logging.getLogger("navig_email")

DRAFT = "DRAFT"
PENDING_APPROVAL = "PENDING_APPROVAL"
SCHEDULED = "SCHEDULED"
CLAIMED = "CLAIMED"
SENDING = "SENDING"
SENT = "SENT"
CANCELLED = "CANCELLED"
FAILED = "FAILED"
NEEDS_REVIEW = "NEEDS_REVIEW"
OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"

STATES = (DRAFT, PENDING_APPROVAL, SCHEDULED, CLAIMED, SENDING, SENT, CANCELLED, FAILED,
          NEEDS_REVIEW, OUTCOME_UNKNOWN)
TERMINAL = (SENT, CANCELLED)
OPEN = tuple(s for s in STATES if s not in TERMINAL)

TRANSITIONS: dict[str, tuple[str, ...]] = {
    DRAFT: (PENDING_APPROVAL, CANCELLED, DRAFT),
    PENDING_APPROVAL: (SCHEDULED, DRAFT, PENDING_APPROVAL, CANCELLED),
    SCHEDULED: (CLAIMED, SCHEDULED, PENDING_APPROVAL, CANCELLED, NEEDS_REVIEW),
    CLAIMED: (SENDING, SCHEDULED, NEEDS_REVIEW, CANCELLED, FAILED),
    SENDING: (SENT, FAILED, OUTCOME_UNKNOWN, SCHEDULED),
    OUTCOME_UNKNOWN: (SENT, NEEDS_REVIEW, OUTCOME_UNKNOWN),
    NEEDS_REVIEW: (PENDING_APPROVAL, CANCELLED, SENT),
    FAILED: (PENDING_APPROVAL, CANCELLED),
    SENT: (),
    CANCELLED: (),
}
#: States in which the text may still change (any change drops the approval).
EDITABLE = (DRAFT, PENDING_APPROVAL, SCHEDULED, NEEDS_REVIEW, FAILED)
CANCELLABLE = (DRAFT, PENDING_APPROVAL, SCHEDULED, CLAIMED, NEEDS_REVIEW, FAILED)

DEFAULT_DELAY = timedelta(minutes=15)
#: A send whose time passed while the computer was off is still sent on reconnect — unless it
#: is this late; then it goes back to a person.
MAX_LATE = timedelta(hours=24)
#: A CLAIMED row older than this was left by a dead dispatcher (nothing was sent: SENDING was
#: never reached) and is released back to SCHEDULED.
CLAIM_STALE = timedelta(minutes=5)
#: A SENDING row older than this died mid-call — its outcome is unknown.
SENDING_STALE = timedelta(minutes=5)
#: How long, and how often, Sent is searched for an OUTCOME_UNKNOWN job before a person decides.
RECONCILE_WINDOW = timedelta(minutes=30)
RECONCILE_EVERY = timedelta(minutes=1)
RETRY_BACKOFF = timedelta(minutes=2)
MAX_RECIPIENTS = 20

PAUSE_KEY = "pause"  # "" | "auto" (policy sends only) | "all"
PAUSE_LEVELS = ("", "auto", "all")


class OutboxError(RuntimeError):
    """A request the outbox refuses (bad recipient, wrong state, stale approval…)."""


class Conflict(OutboxError):
    """The job changed under the caller — another writer won the race."""


# ── time ────────────────────────────────────────────────────────────────────


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


_DUR = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.IGNORECASE)
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def parse_delay(text: str) -> timedelta:
    """``5m`` / ``15m`` / ``1h`` / ``2d`` → a timedelta. Raises ``OutboxError`` otherwise."""
    m = _DUR.match(text or "")
    if not m:
        raise OutboxError(f"not a duration: {text!r} (use 5m, 15m, 1h, 2d)")
    return timedelta(**{_UNITS[m.group(2).lower()]: int(m.group(1))})


def parse_when(text: str, *, now: datetime | None = None) -> datetime:
    """``now`` · a duration (``15m``) · an ISO time (local when it has no zone) → UTC datetime."""
    base = now or now_utc()
    t = (text or "").strip()
    if not t or t.lower() == "now":
        return base
    if _DUR.match(t):
        return base + parse_delay(t)
    try:
        dt = datetime.fromisoformat(t)
    except ValueError as exc:
        raise OutboxError(f"not a time: {text!r} (use 15m, 1h, or 2026-10-20T09:00)") from exc
    if dt.tzinfo is None:
        dt = dt.astimezone()  # a wall-clock time the person typed: their local zone
    return dt.astimezone(timezone.utc)


# ── content ─────────────────────────────────────────────────────────────────


def _clean_header(value: str, field: str) -> str:
    v = (value or "").strip()
    if "\r" in v or "\n" in v:
        raise OutboxError(f"{field} contains a line break")
    return v


def normalize_recipients(value: str, field: str = "to") -> str:
    """``"A <a@x>, b@y"`` → ``"A <a@x>, b@y"`` validated: real addresses, no injection, a cap."""
    raw = _clean_header(value, field)
    if not raw:
        return ""
    pairs = getaddresses([raw])
    out: list[str] = []
    for name, addr in pairs:
        a = addr.strip()
        if not re.fullmatch(r"[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+", a):
            raise OutboxError(f"{field}: not an email address: {addr or name!r}")
        out.append(f"{name} <{a}>" if name else a)
    if len(out) > MAX_RECIPIENTS:
        raise OutboxError(f"{field}: {len(out)} recipients — the limit is {MAX_RECIPIENTS}")
    return ", ".join(out)


def content_hash(job: dict[str, Any]) -> str:
    """What an approval binds to: every field that changes what the recipient receives."""
    parts = [
        job.get(k) or ""
        for k in ("from_addr", "from_name", "to_addrs", "cc_addrs", "subject", "body", "in_reply_to", "thread_id")
    ]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def _message_id(job_id: str, from_addr: str) -> str:
    domain = (from_addr.split("@")[-1] if "@" in from_addr else "") or "navig.local"
    return f"<navig.{job_id}@{domain}>"


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


# ── hooks other modules plug into ──────────────────────────────────────────

#: Extra ``(job) -> reason`` callables run at dispatch; a non-empty reason sends the job to
#: NEEDS_REVIEW. (The policy check is NOT one of these — :func:`revalidate` calls it directly.)
REVALIDATORS: list[Callable[[dict[str, Any]], str]] = []

#: ``(event, job, note)`` — the notifier. Replaced in tests; the default posts to navig's notify router.
Notifier = Callable[[str, dict[str, Any], str], None]


def _default_notifier(event: str, job: dict[str, Any], note: str) -> None:
    from navig_email import mail_notify

    mail_notify.emit(event, job, note)


NOTIFIER: Notifier = _default_notifier


def _notify(event: str, job: dict[str, Any] | None, note: str = "") -> None:
    if job is None:
        return
    try:
        NOTIFIER(event, job, note)
    except Exception:  # noqa: BLE001 — a notification problem never changes a job's state
        logger.debug("outbox notify %s failed", event, exc_info=True)


# ── the store operations ───────────────────────────────────────────────────


def _store(store: MailroomStore | None) -> MailroomStore:
    return store or get_store()


def get(job_id: str, store: MailroomStore | None = None) -> dict[str, Any] | None:
    s = _store(store)
    row = s._read_one("SELECT * FROM outbox WHERE job_id = ?", (job_id,))
    if row is None and len(job_id) >= 4:  # a unique prefix, as typed from a table
        rows = s._read_all("SELECT * FROM outbox WHERE job_id LIKE ? LIMIT 2", (job_id + "%",))
        row = rows[0] if len(rows) == 1 else None
    return dict(row) if row is not None else None


def require(job_id: str, store: MailroomStore | None = None) -> dict[str, Any]:
    job = get(job_id, store)
    if job is None:
        raise OutboxError(f"no outbox job {job_id!r}")
    return job


def list_jobs(
    *, states: tuple[str, ...] | list[str] | None = None, account: str = "", limit: int = 100,
    store: MailroomStore | None = None,
) -> list[dict[str, Any]]:
    s = _store(store)
    where, params = [], []
    if states:
        where.append(f"state IN ({','.join('?' * len(states))})")
        params.extend(states)
    if account:
        where.append("account = ?")
        params.append(account.lower())
    sql = "SELECT * FROM outbox" + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY COALESCE(not_before, created_at) DESC LIMIT ?"
    return [dict(r) for r in s._read_all(sql, (*params, limit))]


def events(job_id: str, store: MailroomStore | None = None) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in _store(store)._read_all("SELECT * FROM outbox_events WHERE job_id = ? ORDER BY id", (job_id,))
    ]


def _event(conn, job_id: str, actor: str, frm: str, to: str, note: str = "") -> None:
    conn.execute(
        "INSERT INTO outbox_events (job_id, at, actor, from_state, to_state, note) VALUES (?, ?, ?, ?, ?, ?)",
        (job_id, iso(now_utc()), actor, frm, to, note[:500]),
    )


def _transition(
    s: MailroomStore,
    job: dict[str, Any],
    to_state: str,
    *,
    actor: str,
    note: str = "",
    **sets: Any,
) -> dict[str, Any]:
    """The only way a job changes. Raises ``Conflict`` when the row moved since *job* was read."""
    frm = job["state"]
    if to_state not in TRANSITIONS.get(frm, ()):
        raise OutboxError(f"job {job['job_id'][:8]} is {frm}; it cannot become {to_state}")
    cols = {"state": to_state, "updated_at": iso(now_utc()), **sets}
    assign = ", ".join(f"{k} = ?" for k in cols)
    with s.transaction() as conn:
        cur = conn.execute(
            f"UPDATE outbox SET {assign}, version = version + 1 WHERE job_id = ? AND state = ? AND version = ?",
            (*cols.values(), job["job_id"], frm, job["version"]),
        )
        if cur.rowcount != 1:
            raise Conflict(f"job {job['job_id'][:8]} changed meanwhile (was {frm} v{job['version']})")
        _event(conn, job["job_id"], actor, frm, to_state, note)
    out = dict(job)
    out.update(cols)
    out["version"] = job["version"] + 1
    return out


def create(
    *,
    account: str,
    to: str,
    subject: str,
    body: str,
    from_addr: str = "",
    from_name: str = "",
    cc: str = "",
    identity: str = "",
    thread_id: str = "",
    reply_to_msg: str = "",
    in_reply_to: str = "",
    references: str = "",
    origin: str = "user",
    policy_id: str = "",
    provider: str = "gmail",
    space_root: str = "",
    style: str = "",
    model: str = "",
    quality: dict[str, Any] | None = None,
    op_key: str = "",
    paths: Any = None,
    actor: str = "user",
    store: MailroomStore | None = None,
) -> dict[str, Any]:
    """Record a new outgoing message as a DRAFT. Idempotent on *op_key*."""
    from navig_email import senders

    s = _store(store)
    acct = (account or "").strip().lower()
    if not acct:
        raise OutboxError("an outgoing message needs the account it is sent from")
    sender = (from_addr or acct).strip().lower()
    if provider == "gmail" and sender != acct:
        if paths is None:
            raise OutboxError(f"sending as {sender} needs the space whose accounts.yaml lists it (--space)")
        if not senders.is_authorized(paths, acct, sender):
            raise OutboxError(f"{sender} is not an address of {acct} (mailroom/accounts.yaml)")
    if provider not in ("gmail", "edge"):
        raise OutboxError(f"unknown provider {provider!r}")
    if not (body or "").strip():
        raise OutboxError("empty body")

    job_id = uuid.uuid4().hex
    stamp = iso(now_utc())
    job: dict[str, Any] = {
        "job_id": job_id,
        "origin": origin,
        "policy_id": policy_id,
        "provider": provider,
        "space_root": space_root or (str(paths.space_root) if paths is not None else ""),
        "account": acct,
        "from_addr": _clean_header(sender, "from"),
        "from_name": _clean_header(from_name, "from name"),
        "identity": identity,
        "to_addrs": normalize_recipients(to, "to"),
        "cc_addrs": normalize_recipients(cc, "cc"),
        "subject": _clean_header(subject, "subject"),
        "body": body,
        "thread_id": thread_id,
        "reply_to_msg": reply_to_msg,
        "in_reply_to": _clean_header(in_reply_to, "in-reply-to"),
        "refs": _clean_header(references or in_reply_to, "references"),
        "message_id": _message_id(job_id, sender),
        "style": style,
        "model": model,
        "quality_json": _dumps(quality) if quality else "",
        "state": DRAFT,
        "version": 1,
        "created_at": stamp,
        "updated_at": stamp,
    }
    if not job["to_addrs"]:
        raise OutboxError("no recipient")
    job["content_hash"] = content_hash(job)
    # Automatic producers (a policy reacting to one incoming message) get a deterministic key,
    # so reprocessing the same mail can never queue a second reply. A person asking twice
    # means twice: their jobs are keyed by the job id unless they pass one.
    if op_key:
        job["op_key"] = op_key
    elif origin == "user":
        job["op_key"] = f"user:{job_id}"
    else:
        job["op_key"] = hashlib.sha256(
            "\x1f".join([acct, thread_id, reply_to_msg, origin, policy_id]).encode()
        ).hexdigest()

    with s.transaction() as conn:
        existing = conn.execute("SELECT * FROM outbox WHERE op_key = ?", (job["op_key"],)).fetchone()
        if existing is not None:
            return dict(existing)
        cols = ", ".join(job)
        conn.execute(f"INSERT INTO outbox ({cols}) VALUES ({', '.join('?' * len(job))})", tuple(job.values()))
        _event(conn, job_id, actor, "", DRAFT, f"created by {origin}")
        s.audit("outbox.create", actor=actor, account=acct, ref=job_id,
                summary=f"{origin} → {_redact(job['to_addrs'])} · {job['subject'][:80]}", conn=conn)
    _notify("draft", job)
    return job


def _dumps(value: Any) -> str:
    from navig_email.store import dumps

    return dumps(value)


def _redact(addrs: str) -> str:
    """Addresses keep their domain only in the audit trail: ``j***@example.com``."""
    out = []
    for _n, a in getaddresses([addrs or ""]):
        local, _, dom = a.partition("@")
        out.append(f"{local[:1]}***@{dom}" if dom else "***")
    return ", ".join(out)


def submit(job_id: str, *, actor: str = "user", store: MailroomStore | None = None) -> dict[str, Any]:
    s = _store(store)
    job = require(job_id, s)
    if job["state"] == PENDING_APPROVAL:
        return job
    job = _transition(s, job, PENDING_APPROVAL, actor=actor, note="submitted for approval")
    _notify("pending_approval", job)
    return job


def edit(
    job_id: str,
    *,
    body: str | None = None,
    subject: str | None = None,
    to: str | None = None,
    cc: str | None = None,
    actor: str = "user",
    store: MailroomStore | None = None,
) -> dict[str, Any]:
    """Change the text. Any change drops the approval: the new version must be approved again."""
    s = _store(store)
    job = require(job_id, s)
    if job["state"] not in EDITABLE:
        raise OutboxError(f"job {job['job_id'][:8]} is {job['state']}; it can no longer be edited")
    updated = dict(job)
    if body is not None:
        if not body.strip():
            raise OutboxError("empty body")
        updated["body"] = body
    if subject is not None:
        updated["subject"] = _clean_header(subject, "subject")
    if to is not None:
        updated["to_addrs"] = normalize_recipients(to, "to")
        if not updated["to_addrs"]:
            raise OutboxError("no recipient")
    if cc is not None:
        updated["cc_addrs"] = normalize_recipients(cc, "cc")
    new_hash = content_hash(updated)
    if new_hash == job["content_hash"]:
        return job
    target = DRAFT if job["state"] == DRAFT else PENDING_APPROVAL
    job = _transition(
        s, job, target, actor=actor, note="edited — approval reset",
        body=updated["body"], subject=updated["subject"], to_addrs=updated["to_addrs"],
        cc_addrs=updated["cc_addrs"], content_hash=new_hash, approved_hash="", approved_by="",
        approved_at=None, not_before=None, quality_json="",
    )
    _notify("modified", job)
    return job


def approve(
    job_id: str,
    *,
    content_hash_seen: str,
    send_at: datetime | None = None,
    actor: str = "user",
    store: MailroomStore | None = None,
) -> dict[str, Any]:
    """Approve the exact version *content_hash_seen* and schedule it (default: in 15 minutes)."""
    s = _store(store)
    job = require(job_id, s)
    if job["state"] == DRAFT:
        job = _transition(s, job, PENDING_APPROVAL, actor=actor, note="submitted for approval")
    if job["state"] != PENDING_APPROVAL:
        raise OutboxError(f"job {job['job_id'][:8]} is {job['state']}; only a job awaiting approval can be approved")
    if not content_hash_seen or content_hash_seen != job["content_hash"]:
        raise Conflict("the text changed since you read it — review the new version first")
    when = send_at or (now_utc() + DEFAULT_DELAY)
    job = _transition(
        s, job, SCHEDULED, actor=actor, note=f"approved; sends at {iso(when)}",
        approved_hash=job["content_hash"], approved_by=actor, approved_at=iso(now_utc()), not_before=iso(when),
    )
    s.audit("outbox.approve", actor=actor, account=job["account"], ref=job["job_id"], summary=f"sends at {iso(when)}")
    _notify("scheduled", job)
    return job


def reschedule(
    job_id: str, send_at: datetime, *, actor: str = "user", store: MailroomStore | None = None
) -> dict[str, Any]:
    """Delay, or bring forward (``send_at=now`` is "send now"). Approval is kept: the text is the same."""
    s = _store(store)
    job = require(job_id, s)
    if job["state"] != SCHEDULED:
        raise OutboxError(f"job {job['job_id'][:8]} is {job['state']}; only a scheduled job can be moved")
    job = _transition(s, job, SCHEDULED, actor=actor, note=f"moved to {iso(send_at)}", not_before=iso(send_at))
    _notify("modified", job)
    return job


def cancel(job_id: str, *, actor: str = "user", store: MailroomStore | None = None) -> dict[str, Any]:
    s = _store(store)
    job = require(job_id, s)
    if job["state"] == CANCELLED:
        return job
    if job["state"] not in CANCELLABLE:
        raise OutboxError(
            f"job {job['job_id'][:8]} is {job['state']} — too late to cancel"
            + (" (it is being sent right now)" if job["state"] in (SENDING, OUTCOME_UNKNOWN) else "")
        )
    job = _transition(s, job, CANCELLED, actor=actor, note="cancelled")
    s.audit("outbox.cancel", actor=actor, account=job["account"], ref=job["job_id"])
    _notify("cancelled", job)
    return job


def reopen(job_id: str, *, actor: str = "user", store: MailroomStore | None = None) -> dict[str, Any]:
    """A FAILED or NEEDS_REVIEW job back to a person's approval — never straight to sending."""
    s = _store(store)
    job = require(job_id, s)
    if job["state"] not in (FAILED, NEEDS_REVIEW):
        raise OutboxError(f"job {job['job_id'][:8]} is {job['state']}; only a failed or reviewed job can be reopened")
    return _transition(
        s, job, PENDING_APPROVAL, actor=actor, note="reopened for approval",
        approved_hash="", approved_by="", approved_at=None, not_before=None,
    )


def mark_sent_manually(job_id: str, *, actor: str = "user", store: MailroomStore | None = None) -> dict[str, Any]:
    """A person confirms a NEEDS_REVIEW job did go out (they saw it in Sent)."""
    s = _store(store)
    job = require(job_id, s)
    if job["state"] != NEEDS_REVIEW:
        raise OutboxError(f"job {job['job_id'][:8]} is {job['state']}")
    job = _transition(s, job, SENT, actor=actor, note="confirmed sent by a person", sent_at=iso(now_utc()))
    _notify("sent", job, "confirmed by you")
    return job


# ── pause ───────────────────────────────────────────────────────────────────


def pause_level(store: MailroomStore | None = None) -> str:
    level = _store(store).meta_get(PAUSE_KEY, "")
    return level if level in PAUSE_LEVELS else "all"  # an unreadable value fails closed


def set_pause(level: str, *, actor: str = "user", store: MailroomStore | None = None) -> str:
    if level not in PAUSE_LEVELS:
        raise OutboxError("pause level must be one of: auto, all (or empty to resume)")
    s = _store(store)
    s.meta_set(PAUSE_KEY, level)
    s.audit("outbox.pause", actor=actor, summary=level or "resumed")
    return level


def _paused_for(job: dict[str, Any], level: str) -> bool:
    return level == "all" or (level == "auto" and job.get("origin") == "policy")


# ── dispatch ────────────────────────────────────────────────────────────────

ConnectorFactory = Callable[[str], Awaitable[Any]]


async def _default_connector(account: str) -> Any:
    from navig_email import account as A

    return await A.connector(account)


CONNECTOR_FACTORY: ConnectorFactory = _default_connector


def _paths_for(job: dict[str, Any]) -> Any:
    root = job.get("space_root") or ""
    if not root:
        return None
    from navig_email.space import MailroomPaths
    from pathlib import Path

    p = Path(root)
    return MailroomPaths(p) if p.is_dir() else None


def _account_ok(account: str) -> bool:
    from navig_email import gmail

    return gmail.is_connected(account)


ACCOUNT_CHECK: Callable[[str], bool] = _account_ok


def revalidate(job: dict[str, Any], *, now: datetime | None = None, store: MailroomStore | None = None) -> str:
    """Why *job* must not be sent right now (``""`` when it may)."""
    from navig_email import senders

    t = now or now_utc()
    if not job.get("approved_hash") or job["approved_hash"] != job["content_hash"] or content_hash(job) != job["content_hash"]:
        return "the approved text does not match the job"
    s = _store(store)
    if s.meta_get(f"account_disabled:{job['account']}", "") == "1":
        return f"account {job['account']} is disconnected from the mailroom"
    nb = parse_iso(job.get("not_before"))
    if nb is not None and t - nb > MAX_LATE:
        return f"it was due {nb:%Y-%m-%d %H:%M} UTC — more than {int(MAX_LATE.total_seconds() // 3600)} h late"
    if job.get("provider") == "gmail":
        paths = _paths_for(job)
        if paths is not None and not senders.is_authorized(paths, job["account"], job["from_addr"]):
            return f"{job['from_addr']} is no longer an address of {job['account']}"
    if job.get("origin") == "policy":
        # Called directly, not through REVALIDATORS: whether the policy module happens to be
        # imported in this process must never decide whether a disabled policy's reply goes out.
        try:
            from navig_email import policy

            reason = policy.still_authorised(job)
        except Exception as exc:  # noqa: BLE001 — cannot check the policy → do not send
            reason = f"the policy could not be checked: {exc}"
        if reason:
            return reason
    for check in REVALIDATORS:
        try:
            reason = check(job)
        except Exception as exc:  # noqa: BLE001 — a check that cannot decide blocks the send
            reason = f"a safety check failed: {exc}"
        if reason:
            return reason
    return ""


def claim_due(
    *, now: datetime | None = None, worker: str = "", limit: int = 10, only: str = "",
    store: MailroomStore | None = None,
) -> list[dict[str, Any]]:
    """Atomically claim up to *limit* due SCHEDULED jobs for *worker*."""
    s = _store(store)
    t = now or now_utc()
    who = worker or worker_id()
    sql = "SELECT * FROM outbox WHERE state = ? AND not_before <= ?"
    params: list[Any] = [SCHEDULED, iso(t)]
    if only:
        sql += " AND job_id = ?"
        params.append(only)
    rows = [dict(r) for r in s._read_all(sql + " ORDER BY not_before LIMIT ?", (*params, limit))]
    claimed = []
    level = pause_level(s)
    for job in rows:
        if _paused_for(job, level):
            continue
        try:
            claimed.append(_transition(s, job, CLAIMED, actor="scheduler", note=f"claimed by {who}",
                                       claimed_by=who, claimed_at=iso(t)))
        except Conflict:
            continue  # cancelled, edited or claimed by someone else between the read and the update
    return claimed


async def dispatch(
    job: dict[str, Any], *, now: datetime | None = None, store: MailroomStore | None = None
) -> dict[str, Any]:
    """Send one CLAIMED job. Returns the job in its new state."""
    s = _store(store)
    reason = revalidate(job, now=now, store=s)
    level = pause_level(s)
    if not reason and _paused_for(job, level):
        return _transition(s, job, SCHEDULED, actor="scheduler", note="released: sending is paused",
                           claimed_by="", claimed_at=None)
    if reason:
        job = _transition(s, job, NEEDS_REVIEW, actor="scheduler", note=reason, last_error=reason)
        _notify("needs_review", job, reason)
        return job

    connector = None
    if job.get("provider") == "gmail":
        if not ACCOUNT_CHECK(job["account"]):
            job = _transition(s, job, NEEDS_REVIEW, actor="scheduler", note="account not connected",
                              last_error="account not connected")
            _notify("auth_expired", job)
            return job
        try:
            connector = await CONNECTOR_FACTORY(job["account"])
        except Exception as exc:  # noqa: BLE001 — no connector: nothing was sent; a person looks
            job = _transition(s, job, NEEDS_REVIEW, actor="scheduler", note=f"no connector: {exc}",
                              last_error=str(exc)[:300])
            _notify("auth_expired", job)
            return job

    # The point after which "did it go out?" can only be answered by the mailbox.
    job = _transition(s, job, SENDING, actor="scheduler", note="sending", sending_at=iso(now_utc()),
                      attempts=int(job.get("attempts") or 0) + 1)
    result = await providers.send(job, connector, paths=_paths_for(job))

    if result.outcome == providers.SENT:
        job = _transition(s, job, SENT, actor="scheduler", note="provider accepted",
                          sent_at=iso(now_utc()), provider_msg_id=result.provider_msg_id, last_error="")
        s.audit("outbox.sent", actor="scheduler", account=job["account"], ref=job["job_id"],
                summary=f"→ {_redact(job['to_addrs'])}")
        _notify("sent", job)
        return job
    if result.outcome == providers.RETRY:
        job = _transition(s, job, SCHEDULED, actor="scheduler", note=f"not sent, will retry: {result.error}",
                          not_before=iso(now_utc() + RETRY_BACKOFF), claimed_by="", claimed_at=None,
                          last_error=result.error)
        return job
    if result.outcome == providers.NOT_SENT:
        job = _transition(s, job, FAILED, actor="scheduler", note=result.error, last_error=result.error)
        _notify("auth_expired" if result.auth_problem else "failed", job, result.error)
        return job
    job = _transition(s, job, OUTCOME_UNKNOWN, actor="scheduler",
                      note=f"outcome unknown: {result.error}", last_error=result.error)
    _notify("outcome_unknown", job, result.error)
    return job


async def reconcile(job: dict[str, Any], *, now: datetime | None = None,
                    store: MailroomStore | None = None) -> dict[str, Any]:
    """Look for an OUTCOME_UNKNOWN job in Sent. Found → SENT. Window over → NEEDS_REVIEW."""
    s = _store(store)
    t = now or now_utc()
    started = parse_iso(job.get("sending_at")) or parse_iso(job.get("updated_at")) or t
    try:
        connector = await CONNECTOR_FACTORY(job["account"]) if job.get("provider") == "gmail" else None
        found = await providers.find_sent(job, connector)
    except Exception as exc:  # noqa: BLE001 — could not look ≠ not sent
        found, err = "", str(exc)[:200]
    else:
        err = ""
    if found:
        job = _transition(s, job, SENT, actor="reconciler", note="found in Sent", sent_at=iso(t),
                          provider_msg_id=found, last_error="")
        _notify("sent", job, "confirmed from Sent")
        return job
    if t - started >= RECONCILE_WINDOW:
        note = "not found in Sent after the reconciliation window — a person decides" + (f" ({err})" if err else "")
        job = _transition(s, job, NEEDS_REVIEW, actor="reconciler", note=note, last_error=note)
        _notify("needs_review", job, note)
        return job
    return _transition(s, job, OUTCOME_UNKNOWN, actor="reconciler",
                       note="not in Sent yet" + (f" ({err})" if err else ""),
                       reconcile_tries=int(job.get("reconcile_tries") or 0) + 1)


def recover_stale(*, now: datetime | None = None, store: MailroomStore | None = None) -> int:
    """After a crash: release dead claims (nothing was sent) and mark dead sends as unknown."""
    s = _store(store)
    t = now or now_utc()
    moved = 0
    for job in [dict(r) for r in s._read_all("SELECT * FROM outbox WHERE state = ?", (CLAIMED,))]:
        at = parse_iso(job.get("claimed_at"))
        if at is None or t - at >= CLAIM_STALE:
            try:
                _transition(s, job, SCHEDULED, actor="recovery", note="released a stale claim",
                            claimed_by="", claimed_at=None)
                moved += 1
            except Conflict:
                pass
    for job in [dict(r) for r in s._read_all("SELECT * FROM outbox WHERE state = ?", (SENDING,))]:
        at = parse_iso(job.get("sending_at"))
        if at is None or t - at >= SENDING_STALE:
            try:
                job = _transition(s, job, OUTCOME_UNKNOWN, actor="recovery",
                                  note="interrupted while sending — checking Sent")
                _notify("outcome_unknown", job, "interrupted while sending")
                moved += 1
            except Conflict:
                pass
    return moved


async def tick(*, now: datetime | None = None, only: str = "", store: MailroomStore | None = None) -> dict[str, int]:
    """One dispatcher pass: recover, send what is due, reconcile what is unknown."""
    s = _store(store)
    t = now or now_utc()
    stats = {"recovered": recover_stale(now=t, store=s), "sent": 0, "failed": 0, "unknown": 0,
             "review": 0, "reconciled": 0}
    for job in claim_due(now=t, only=only, store=s):
        try:
            done = await dispatch(job, now=t, store=s)
        except Conflict:
            continue
        key = {SENT: "sent", FAILED: "failed", OUTCOME_UNKNOWN: "unknown", NEEDS_REVIEW: "review"}.get(done["state"])
        if key:
            stats[key] += 1
    for job in list_jobs(states=[OUTCOME_UNKNOWN], limit=20, store=s):
        last = parse_iso(job.get("updated_at"))
        if only and job["job_id"] != only:
            continue
        if last is not None and t - last < RECONCILE_EVERY and int(job.get("reconcile_tries") or 0) > 0:
            continue
        try:
            done = await reconcile(job, now=t, store=s)
        except Conflict:
            continue
        if done["state"] == SENT:
            stats["reconciled"] += 1
        elif done["state"] == NEEDS_REVIEW:
            stats["review"] += 1
    return stats


def disable_account(account: str, disabled: bool = True, *, actor: str = "user",
                    store: MailroomStore | None = None) -> None:
    """Disconnecting an account from the mailroom stops every pending send for it at dispatch."""
    s = _store(store)
    s.meta_set(f"account_disabled:{account.strip().lower()}", "1" if disabled else "")
    s.audit("account.disable" if disabled else "account.enable", actor=actor, account=account.strip().lower())
