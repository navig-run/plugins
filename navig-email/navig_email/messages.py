"""One flat shape for a Gmail message, whatever endpoint it came from.

The connector hands back raw Gmail JSON (``messages.get`` / ``threads.get``). Every
consumer here — rules, stats, the replied-finder, the ledger — wants the same dozen
fields, so the shaping lives in one place.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import getaddresses, parseaddr
from typing import Any

SYSTEM_LABELS = frozenset(
    {
        "INBOX",
        "SENT",
        "SPAM",
        "TRASH",
        "DRAFT",
        "UNREAD",
        "STARRED",
        "IMPORTANT",
        "CHAT",
        "CATEGORY_PERSONAL",
        "CATEGORY_SOCIAL",
        "CATEGORY_PROMOTIONS",
        "CATEGORY_UPDATES",
        "CATEGORY_FORUMS",
    }
)

# Gmail search folder words → label ids.
FOLDER_LABEL = {
    "inbox": "INBOX",
    "sent": "SENT",
    "spam": "SPAM",
    "trash": "TRASH",
    "draft": "DRAFT",
    "drafts": "DRAFT",
    "starred": "STARRED",
    "unread": "UNREAD",
}


def _header(headers: list[dict[str, str]], name: str) -> str:
    low = name.lower()
    for h in headers or []:
        if str(h.get("name", "")).lower() == low:
            return str(h.get("value", "") or "")
    return ""


def internal_ms(msg: dict[str, Any]) -> int:
    try:
        return int(msg.get("internalDate") or 0)
    except (TypeError, ValueError):
        return 0


def iso_from_ms(ms: int) -> str:
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(
        timespec="seconds"
    )


def edge_tags(header_value: str) -> list[str]:
    """``"support,echeance"`` → ``["support", "echeance"]`` (lowercase, de-duplicated).

    The header is written by the mailroom's Cloudflare Worker before it forwards the
    message, so the Gmail side can label by the triage the edge already performed instead
    of deriving it from the subject a second time.
    """
    out: list[str] = []
    for part in str(header_value or "").split(","):
        tag = part.strip().lower()
        if tag and tag not in out:
            out.append(tag)
    return out


def shape(msg: dict[str, Any], *, body: str = "") -> dict[str, Any]:
    """Raw ``messages.get`` JSON (metadata or full) → flat dict."""
    payload = msg.get("payload") or {}
    headers = payload.get("headers") or []
    ms = internal_ms(msg)
    return {
        "id": str(msg.get("id", "")),
        "thread_id": str(msg.get("threadId", "")),
        "labels": list(msg.get("labelIds") or []),
        "from": _header(headers, "From"),
        "to": _header(headers, "To"),
        "cc": _header(headers, "Cc"),
        "subject": _header(headers, "Subject"),
        "date": iso_from_ms(ms) or _header(headers, "Date"),
        "internal_ms": ms,
        "snippet": str(msg.get("snippet", "") or ""),
        "message_id": _header(headers, "Message-ID"),
        "in_reply_to": _header(headers, "In-Reply-To"),
        # From the mailroom edge Worker (both empty for mail that never passed through it).
        "tags": edge_tags(_header(headers, "X-Cybesis-Tag")),
        "alias": _header(headers, "X-Cybesis-Alias").strip().lower(),
        "has_attachment": _has_attachment(payload),
        "body": body,
        "url": f"https://mail.google.com/mail/#all/{msg.get('id', '')}",
    }


def _has_attachment(payload: dict[str, Any]) -> bool:
    if payload.get("filename"):
        return True
    return any(_has_attachment(p) for p in payload.get("parts") or [])


def address(value: str) -> str:
    """``"Name <a@b.c>"`` → ``a@b.c`` (lowercase)."""
    _name, addr = parseaddr(value or "")
    return (addr or value or "").strip().lower()


def addresses(value: str) -> list[str]:
    return [a.strip().lower() for _n, a in getaddresses([value or ""]) if a]


def display_name(value: str) -> str:
    name, addr = parseaddr(value or "")
    return (name or addr or value or "").strip()


def domain_of(value: str) -> str:
    addr = address(value)
    return addr.rsplit("@", 1)[-1] if "@" in addr else ""


def is_mine(msg: dict[str, Any], my_email: str = "") -> bool:
    """Sent by the connected account: the SENT label, or From matches the account."""
    if "SENT" in (msg.get("labels") or []):
        return True
    return bool(my_email) and address(msg.get("from", "")) == my_email.lower()


_RE_PREFIX = re.compile(r"^\s*(?:(?:re|fwd?|tr|aw|sv)\s*:\s*)+", re.I)


def bare_subject(subject: str) -> str:
    return _RE_PREFIX.sub("", subject or "").strip()


def clean_body(text: str, *, max_chars: int = 4000) -> str:
    """Drop quoted history and signatures-of-the-quoted; keep the author's own words."""
    if not text:
        return ""
    out: list[str] = []
    for line in text.splitlines():
        s = line.rstrip()
        if s.startswith(">"):
            continue
        low = s.lower()
        if re.match(r"^(le|on) .{4,80}(a écrit|a ecrit|wrote)\s*:?$", low):
            break
        if low.startswith(
            (
                "-----original message",
                "---------- forwarded",
                "de :",
                "from:",
                "envoyé :",
                "sent:",
            )
        ):
            break
        out.append(s)
    text = "\n".join(out).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:max_chars]
