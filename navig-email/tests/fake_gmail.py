"""An in-memory Gmail: the subset of ``GmailConnector`` the mailroom uses.

Messages are raw Gmail-shaped dicts (``payload.headers``, ``labelIds``,
``internalDate``, ``threadId``) so ``messages.shape`` and ``_extract_body`` run
unchanged. Search supports the handful of operators the plugin emits.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from typing import Any

from navig.connectors.errors import ConnectorAPIError
from navig.connectors.gmail.connector import GmailConnector


def ms(iso: str) -> int:
    return int(
        datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp() * 1000
    )


def raw_message(
    mid: str,
    *,
    thread: str,
    frm: str,
    to: str,
    subject: str,
    date: str,
    labels: list[str],
    body: str = "",
    message_id: str | None = None,
    in_reply_to: str = "",
    snippet: str | None = None,
    attachment: bool = False,
) -> dict[str, Any]:
    headers = [
        {"name": "From", "value": frm},
        {"name": "To", "value": to},
        {"name": "Subject", "value": subject},
        {"name": "Message-ID", "value": message_id or f"<{mid}@x>"},
    ]
    if in_reply_to:
        headers.append({"name": "In-Reply-To", "value": in_reply_to})
    payload: dict[str, Any] = {
        "mimeType": "text/plain",
        "headers": headers,
        "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()},
    }
    if attachment:
        payload = {
            "mimeType": "multipart/mixed",
            "headers": headers,
            "parts": [
                payload | {"headers": []},
                {"mimeType": "application/pdf", "filename": "x.pdf", "body": {}},
            ],
        }
    return {
        "id": mid,
        "threadId": thread,
        "labelIds": list(labels),
        "internalDate": str(ms(date)),
        "snippet": snippet if snippet is not None else body[:80],
        "payload": payload,
    }


class FakeGmail:
    """Duck-types the connector. ``history`` is a list of (historyId, message id) events."""

    def __init__(self, email: str = "me@gmail.com"):
        self.email = email
        self.messages: dict[str, dict[str, Any]] = {}
        self.labels: dict[str, dict[str, Any]] = {
            lid: {"id": lid, "name": lid, "type": "system"}
            for lid in ("INBOX", "SENT", "SPAM", "UNREAD", "STARRED", "TRASH", "DRAFT")
        }
        self.history: list[tuple[int, str]] = []
        self.history_id = 1000
        self.drafts: list[dict[str, Any]] = []
        self.calls: list[tuple[str, Any]] = []
        self.stale_history = False

    # ── fixture helpers ──
    def add(self, msg: dict[str, Any]) -> dict[str, Any]:
        self.messages[msg["id"]] = msg
        self.history_id += 1
        self.history.append((self.history_id, msg["id"]))
        return msg

    def _labels_of(self, m):
        return set(m.get("labelIds") or [])

    def _matches(self, m: dict[str, Any], query: str) -> bool:
        labels = self._labels_of(m)
        hdr = {h["name"].lower(): h["value"] for h in m["payload"]["headers"]}
        for tok in query.split():
            neg = tok.startswith("-")
            t = tok[1:] if neg else tok
            ok = True
            if t.startswith("in:"):
                ok = t[3:].upper() in labels
            elif t.startswith("is:"):
                ok = t[3:].upper() in labels
            elif t.startswith("after:"):
                ok = int(m["internalDate"]) >= int(t[6:]) * 1000
            elif t.startswith("before:"):
                ok = int(m["internalDate"]) < int(t[7:]) * 1000
            elif t.startswith("to:"):
                ok = t[3:].lower() in hdr.get("to", "").lower()
            elif t.startswith("from:"):
                ok = t[5:].lower() in hdr.get("from", "").lower()
            elif t.startswith("newer_than:"):
                ok = True
            elif t.startswith("has:attachment"):
                ok = any(p.get("filename") for p in m["payload"].get("parts") or [])
            elif t.startswith("label:"):
                ok = t[6:].lower() in {
                    v["name"].lower() for k, v in self.labels.items() if k in labels
                }
            else:
                ok = t.lower() in (hdr.get("subject", "") + hdr.get("from", "")).lower()
            if neg:
                ok = not ok
            if not ok:
                return False
        return True

    # ── connector API ──
    async def get_profile(self):
        return {
            "emailAddress": self.email,
            "historyId": str(self.history_id),
            "messagesTotal": len(self.messages),
            "threadsTotal": len({m["threadId"] for m in self.messages.values()}),
        }

    async def iter_message_ids(
        self, query="", *, limit=None, label_ids=None, include_spam_trash=False
    ):
        self.calls.append(("iter", query))
        out = []
        for m in sorted(self.messages.values(), key=lambda x: -int(x["internalDate"])):
            labels = self._labels_of(m)
            if (
                not include_spam_trash
                and (labels & {"SPAM", "TRASH"})
                and "in:spam" not in query
                and "in:trash" not in query
            ):
                continue
            if label_ids and not set(label_ids) <= labels:
                continue
            if query and not self._matches(m, query):
                continue
            out.append({"id": m["id"], "threadId": m["threadId"]})
        return out[:limit] if limit else out

    async def get_message(self, mid, *, format="metadata", headers=None):
        if mid not in self.messages:
            raise ConnectorAPIError("gmail", 404, "not found")
        return self.messages[mid]

    async def get_many(self, ids, *, format="metadata", headers=None, concurrency=8):
        return [self.messages[i] for i in ids if i in self.messages]

    async def get_thread(self, tid, *, format="metadata", headers=None):
        msgs = [m for m in self.messages.values() if m["threadId"] == tid]
        if not msgs:
            raise ConnectorAPIError("gmail", 404, "no thread")
        return {
            "id": tid,
            "messages": sorted(msgs, key=lambda x: int(x["internalDate"])),
        }

    async def get_threads(
        self, tids, *, format="metadata", headers=None, concurrency=8
    ):
        out = []
        for t in tids:
            try:
                out.append(await self.get_thread(t))
            except ConnectorAPIError:
                pass
        return out

    async def list_history(
        self, start, *, label_id=None, history_types=("messageAdded",), page_token=None
    ):
        if self.stale_history:
            raise ConnectorAPIError("gmail", 404, "startHistoryId too old")
        items = [
            {
                "messagesAdded": [
                    {
                        "message": {
                            "id": mid,
                            "threadId": self.messages[mid]["threadId"],
                            "labelIds": self.messages[mid]["labelIds"],
                        }
                    }
                ]
            }
            for hid, mid in self.history
            if hid > int(start) and mid in self.messages
        ]
        return {"history": items, "historyId": str(self.history_id)}

    async def list_labels(self):
        return list(self.labels.values())

    async def get_label(self, lid):
        return self.labels[lid]

    async def create_label(self, name):
        lid = f"Label_{len(self.labels)}"
        self.labels[lid] = {"id": lid, "name": name, "type": "user"}
        return self.labels[lid]

    async def ensure_label(self, name):
        for lid, lbl in self.labels.items():
            if lbl["name"].lower() == name.lower() or lid == name:
                return lid
        parts = [p for p in name.split("/") if p]
        lid = ""
        for d in range(1, len(parts) + 1):
            partial = "/".join(parts[:d])
            found = next(
                (
                    k
                    for k, v in self.labels.items()
                    if v["name"].lower() == partial.lower()
                ),
                None,
            )
            lid = found or (await self.create_label(partial))["id"]
        return lid

    async def modify_message(self, mid, *, add=None, remove=None):
        from navig.connectors.types import ActionResult

        self.calls.append(("modify", mid, tuple(add or ()), tuple(remove or ())))
        m = self.messages.get(mid)
        if m is None:
            return ActionResult(success=False, error="404")
        labels = self._labels_of(m)
        labels |= set(add or ())
        labels -= set(remove or ())
        m["labelIds"] = sorted(labels)
        return ActionResult(success=True)

    async def modify_thread(self, tid, *, add=None, remove=None):
        from navig.connectors.types import ActionResult

        for m in self.messages.values():
            if m["threadId"] == tid:
                await self.modify_message(m["id"], add=add, remove=remove)
        return ActionResult(success=True)

    async def create_draft(
        self, *, to, subject, body, thread_id=None, in_reply_to=None, references=None
    ):
        d = {
            "id": f"d{len(self.drafts) + 1}",
            "to": to,
            "subject": subject,
            "body": body,
            "threadId": thread_id,
            "in_reply_to": in_reply_to,
        }
        self.drafts.append(d)
        return d

    _extract_body = staticmethod(GmailConnector._extract_body)
