"""Turn one approved outbox job into one provider call — and say honestly what happened.

The single most important property here is the **three-way outcome**. A provider call does not
end in "worked / didn't": it can be *refused before acceptance* (nothing went out — safe to try
again later), *accepted* (it went out), or *lost in the middle* (a timeout after the request
left — it may or may not have gone out). The third case is never retried; the outbox parks it in
``OUTCOME_UNKNOWN`` and :func:`find_sent` looks for it in the mailbox's Sent folder by the
``Message-ID`` the outbox stamped on it *before* sending. That id is what makes a retry decision
evidence-based instead of a guess.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import smtplib
import socket
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from typing import Any

logger = logging.getLogger("navig_email")

SENT = "sent"
NOT_SENT = "not_sent"  # refused before acceptance: nothing left; a later retry is safe
RETRY = "retry"  # transient refusal before acceptance (rate limit, could not connect)
UNKNOWN = "unknown"  # the request may have been accepted — never retry blindly


@dataclass
class SendResult:
    outcome: str
    provider_msg_id: str = ""
    error: str = ""
    auth_problem: bool = False


def build_mime(job: dict[str, Any]) -> EmailMessage:
    """The exact message for *job*. Headers come only from the job row (validated at enqueue)."""
    msg = EmailMessage()
    sender = job.get("from_addr") or job.get("account") or ""
    msg["From"] = formataddr((job.get("from_name") or "", sender)) if job.get("from_name") else sender
    msg["To"] = job.get("to_addrs") or ""
    if job.get("cc_addrs"):
        msg["Cc"] = job["cc_addrs"]
    msg["Subject"] = job.get("subject") or ""
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = job["message_id"]
    if job.get("in_reply_to"):
        msg["In-Reply-To"] = job["in_reply_to"]
        msg["References"] = job.get("refs") or job["in_reply_to"]
    msg["X-Navig-Job"] = job["job_id"]
    msg.set_content(job.get("body") or "", charset="utf-8")
    return msg


def _classify(exc: BaseException) -> SendResult:
    """Map a provider exception to an outcome. When unsure: UNKNOWN."""
    name = type(exc).__name__
    text = str(exc)[:300]
    status = getattr(exc, "status_code", None)
    if name == "ConnectorAuthError":
        return SendResult(NOT_SENT, error=text, auth_problem=True)
    if isinstance(status, int):
        if status == 429:
            return SendResult(RETRY, error=text)
        if status in (401, 403):
            return SendResult(NOT_SENT, error=text, auth_problem=True)
        if 400 <= status < 500:
            return SendResult(NOT_SENT, error=text)
        return SendResult(UNKNOWN, error=text)
    # httpx: a connection that never opened sent nothing; anything later is ambiguous.
    if name in ("ConnectError", "ConnectTimeout"):
        return SendResult(RETRY, error=text)
    if isinstance(exc, (smtplib.SMTPAuthenticationError,)):
        return SendResult(NOT_SENT, error=text, auth_problem=True)
    if isinstance(
        exc,
        (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPHeloError, smtplib.SMTPNotSupportedError),
    ):
        return SendResult(NOT_SENT, error=text)
    if isinstance(exc, smtplib.SMTPDataError):
        code = getattr(exc, "smtp_code", 0) or 0
        return SendResult(NOT_SENT if 500 <= code < 600 else RETRY if 400 <= code < 500 else UNKNOWN, error=text)
    if isinstance(exc, (ConnectionRefusedError, socket.gaierror)):
        return SendResult(RETRY, error=text)
    return SendResult(UNKNOWN, error=f"{name}: {text}")


async def _gmail_send(connector: Any, mime: EmailMessage, thread_id: str) -> str:
    raw = mime.as_bytes()
    send_raw = getattr(connector, "send_raw", None)
    if callable(send_raw):
        data = await send_raw(raw, thread_id=thread_id or None)
    else:  # an older core: the same endpoint, through the connector's own authorised client
        body: dict[str, Any] = {"raw": base64.urlsafe_b64encode(raw).decode("ascii")}
        if thread_id:
            body["threadId"] = thread_id
        data = await connector._api_post("/users/me/messages/send", json_body=body)
    return str((data or {}).get("id") or "")


async def send(job: dict[str, Any], connector: Any = None, *, paths: Any = None) -> SendResult:
    """One attempt. Never raises; the outcome says what the caller may do next."""
    try:
        if job.get("provider") == "edge":
            from navig_email import edge as E

            res = await asyncio.to_thread(
                E.send,
                paths,
                to=job.get("to_addrs") or "",
                subject=job.get("subject") or "",
                text=job.get("body") or "",
                from_addr=job.get("from_addr") or None,
                from_name=job.get("from_name") or None,
                in_reply_to=job.get("in_reply_to") or None,
            )
            rid = str(((res or {}).get("result") or {}).get("messageId") or (res or {}).get("id") or "")
            return SendResult(SENT, provider_msg_id=rid)
        if connector is None:
            return SendResult(NOT_SENT, error="the account is not connected", auth_problem=True)
        mid = await _gmail_send(connector, build_mime(job), job.get("thread_id") or "")
        return SendResult(SENT, provider_msg_id=mid)
    except RuntimeError as exc:
        # edge.send raises RuntimeError("edge send failed (4xx): …") on an HTTP refusal.
        text = str(exc)
        if "failed (4" in text:
            return SendResult(NOT_SENT, error=text[:300])
        return _classify(exc)
    except Exception as exc:  # noqa: BLE001 — every failure is classified, none escapes
        return _classify(exc)


async def find_sent(job: dict[str, Any], connector: Any) -> str:
    """The provider id of the message carrying *job*'s Message-ID, or ``""`` when not found.

    Raises when the lookup itself fails — "could not look" must never read as "not sent".
    """
    if connector is None or job.get("provider") == "edge":
        raise LookupError("no mailbox to search for this job")
    mid = (job.get("message_id") or "").strip("<>")
    if not mid:
        raise LookupError("job has no Message-ID")
    found = await connector.iter_message_ids(f"rfc822msgid:{mid}", limit=1, include_spam_trash=True)
    return str(found[0]["id"]) if found else ""
