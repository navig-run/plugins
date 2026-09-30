"""Gmail over IMAP — the mailroom's backend when navig (and its OAuth connector) is absent.

Every mailroom command talks to "a Gmail connector": navig's OAuth ``GmailConnector`` inside
navig. On its own there is no OAuth app to talk through, but Gmail's IMAP speaks Gmail:

=====================  ==========================================================
Gmail API              Gmail IMAP (X-GM-EXT-1)
=====================  ==========================================================
``q=`` search syntax   ``X-GM-RAW`` — the same syntax, verbatim
message / thread id    ``X-GM-MSGID`` / ``X-GM-THRID`` — the API ids ARE their hex
labels                 ``X-GM-LABELS`` (+ ``\\Seen``/``\\Flagged`` for UNREAD/STARRED)
spam / trash / all     the ``\\Junk`` / ``\\Trash`` / ``\\All`` SPECIAL-USE mailboxes
body + headers         the RFC 822 source, shaped into the API's ``payload``
drafts / send          ``APPEND`` to ``\\Drafts`` / SMTP with the same app password
=====================  ==========================================================

So :class:`GmailImap` implements the SAME methods the commands call on the OAuth connector,
returning the SAME raw-Gmail-JSON shapes, and every command runs unchanged. Sign-in needs a
Google **app password** (Google Account → Security → App passwords), not an OAuth app.

Only Gmail is supported: a server without ``X-GM-EXT-1`` is refused with a clear message
rather than half-working, because labels and Gmail search are what the mailroom is built on.

Standard library only. imaplib is synchronous and not thread-safe, so every operation runs in
a worker thread under one lock — correct first; the mailroom's volumes do not need more.
"""

from __future__ import annotations

import asyncio
import base64
import email
import email.policy
import html as _html
import imaplib
import re
import smtplib
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from email.header import decode_header, make_header
from email.mime.text import MIMEText
from typing import Any

from navig_email.errors import ActionResult, ActionType, ConnectorAPIError, ConnectorAuthError

__all__ = ["GmailImap", "ImapNotGmail", "decode_mutf7", "encode_mutf7"]

CONNECTOR_ID = "gmail-imap"
IMAP_HOST = "imap.gmail.com"
SMTP_HOST = "smtp.gmail.com"
HISTORY_PREFIX = "imap:"

DEFAULT_METADATA_HEADERS: tuple[str, ...] = (
    "Subject", "From", "To", "Cc", "Date", "Message-ID", "In-Reply-To", "References",
    "X-Cybesis-Tag", "X-Cybesis-Alias",
)

# Gmail's IMAP system labels -> Gmail API label ids.
_SYSTEM_IMAP_TO_API = {
    "\\inbox": "INBOX", "\\sent": "SENT", "\\important": "IMPORTANT",
    "\\starred": "STARRED", "\\draft": "DRAFT", "\\spam": "SPAM", "\\trash": "TRASH",
}
_SYSTEM_API_TO_IMAP = {
    "INBOX": "\\Inbox", "SENT": "\\Sent", "IMPORTANT": "\\Important",
    "DRAFT": "\\Draft", "SPAM": "\\Spam", "TRASH": "\\Trash",
}
SYSTEM_LABEL_IDS = ("INBOX", "SENT", "SPAM", "TRASH", "DRAFT", "UNREAD", "STARRED", "IMPORTANT")


class ImapNotGmail(ConnectorAPIError):
    """The server is not Gmail (no X-GM-EXT-1)."""


# ── modified UTF-7 (RFC 3501 §5.1.3): how IMAP spells non-ASCII mailbox/label names ──


def encode_mutf7(text: str) -> str:
    out: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            raw = "".join(buf).encode("utf-16-be")
            out.append("&" + base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",") + "-")
            buf.clear()

    for ch in text:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def decode_mutf7(text: str) -> str:
    def repl(m: re.Match[str]) -> str:
        body = m.group(1)
        if not body:
            return "&"
        b64 = body.replace(",", "/")
        b64 += "=" * (-len(b64) % 4)
        return base64.b64decode(b64).decode("utf-16-be")

    return re.sub(r"&([A-Za-z0-9+,]*)-", repl, text)


# ── FETCH response parsing ──────────────────────────────────────────────────


def _tokenize_list(text: str, start: int) -> tuple[list[str], int]:
    """Parse an IMAP parenthesised list beginning at text[start] == '('; returns (items, end)."""
    assert text[start] == "("
    items: list[str] = []
    i = start + 1
    while i < len(text):
        c = text[i]
        if c == ")":
            return items, i + 1
        if c == " ":
            i += 1
        elif c == '"':
            j, buf = i + 1, []
            while j < len(text) and text[j] != '"':
                if text[j] == "\\" and j + 1 < len(text):
                    j += 1
                buf.append(text[j])
                j += 1
            items.append("".join(buf))
            i = j + 1
        else:
            j = i
            while j < len(text) and text[j] not in " )":
                j += 1
            items.append(text[i:j])
            i = j
    return items, i


_LITERAL_TAIL = re.compile(r"(BODY\[[^\]]*\](?:<\d+>)?) \{\d+\}$")
_NEW_RECORD = re.compile(r"^\d+ \(")


def parse_fetch(data: list[Any]) -> list[dict[str, Any]]:
    """imaplib ``FETCH`` data -> ``[{"meta": str, "sections": {name: bytes}}]`` per message."""
    records: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for item in data or []:
        if item is None:
            continue
        if isinstance(item, tuple):
            head = item[0].decode("utf-8", "replace") if isinstance(item[0], bytes) else str(item[0])
            if _NEW_RECORD.match(head) or cur is None:
                cur = {"meta": "", "sections": {}}
                records.append(cur)
            m = _LITERAL_TAIL.search(head)
            if m:
                cur["sections"][m.group(1).split("<")[0]] = item[1]
                head = head[: m.start()]
            cur["meta"] += " " + head
        else:
            text = item.decode("utf-8", "replace") if isinstance(item, bytes) else str(item)
            if _NEW_RECORD.match(text):
                cur = {"meta": text, "sections": {}}
                records.append(cur)
            elif cur is not None:
                cur["meta"] += " " + text
    return records


def _meta_int(meta: str, key: str) -> int | None:
    m = re.search(rf"\b{re.escape(key)} (\d+)", meta)
    return int(m.group(1)) if m else None


def _meta_list(meta: str, key: str) -> list[str]:
    idx = meta.find(key + " (")
    if idx < 0:
        return []
    items, _ = _tokenize_list(meta, idx + len(key) + 1)
    return items


def _internal_ms(meta: str) -> int:
    m = re.search(r'INTERNALDATE "([^"]+)"', meta)
    if not m:
        return 0
    try:
        return int(datetime.strptime(m.group(1), "%d-%b-%Y %H:%M:%S %z").timestamp() * 1000)
    except ValueError:
        return 0


# ── HTML / body helpers (mirroring the OAuth connector's) ───────────────────

_HTML_BLOCK_RE = re.compile(r"<(script|style)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_HTML_BREAK_RE = re.compile(r"<\s*(br|/p|/div|/tr|/li|/h[1-6])\s*/?>", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_NL_RE = re.compile(r"\n{3,}")


def html_to_text(markup: str) -> str:
    text = _HTML_BLOCK_RE.sub(" ", markup)
    text = _HTML_BREAK_RE.sub("\n", text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = _html.unescape(text).replace("\xa0", " ")
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _NL_RE.sub("\n\n", text).strip()


def _decode_header_value(value: Any) -> str:
    try:
        return str(make_header(decode_header(str(value))))
    except Exception:  # noqa: BLE001 - a malformed encoded-word reads as the raw value
        return str(value)


def _headers(msg: email.message.Message, only: tuple[str, ...] | None) -> list[dict[str, str]]:
    wanted = {h.lower() for h in only} if only else None
    return [
        {"name": k, "value": _decode_header_value(v)}
        for k, v in msg.items()
        if wanted is None or k.lower() in wanted
    ]


def _payload(part: email.message.Message) -> dict[str, Any]:
    """RFC 822 part -> the Gmail API ``payload`` shape (bodies base64url of UTF-8 text)."""
    out: dict[str, Any] = {
        "mimeType": part.get_content_type(),
        "filename": part.get_filename() or "",
        "headers": _headers(part, None),
    }
    if part.is_multipart():
        out["body"] = {"size": 0}
        out["parts"] = [_payload(p) for p in part.get_payload()]  # type: ignore[union-attr]
        return out
    data = part.get_payload(decode=True) or b""
    if part.get_content_maintype() == "text":
        charset = part.get_content_charset() or "utf-8"
        try:
            data = data.decode(charset, errors="replace").encode("utf-8")
        except LookupError:
            data = data.decode("utf-8", errors="replace").encode("utf-8")
    out["body"] = {"size": len(data), "data": base64.urlsafe_b64encode(data).decode("ascii")}
    return out


def _snippet_from(msg: email.message.Message) -> str:
    text = GmailImap._extract_body(_payload(msg))
    return " ".join(text.split())[:200]


def _partial_snippet(header: bytes, text: bytes, truncated: bool) -> str:
    """Snippet from the header + the first bytes of the body (metadata fetches).

    The body alone is not readable: UTF-8 mail is usually base64 or quoted-printable, and
    showing the raw transfer encoding (``TW9udGFudDog…``) is what the first version did.
    Parsing header + partial body applies the real encoding and charset. The cut-off last
    line is dropped first so a half base64 line cannot poison the decode.
    """
    if truncated and b"\n" in text:
        text = text[: text.rfind(b"\n") + 1]
    try:
        msg = email.message_from_bytes(header.rstrip(b"\r\n") + b"\r\n\r\n" + text, policy=email.policy.compat32)
        snippet = _snippet_from(msg)
    except Exception:  # noqa: BLE001 - a malformed partial body falls back to the crude read
        snippet = ""
    return snippet or _crude_snippet(text)


def _crude_snippet(raw: bytes) -> str:
    """Best effort from the first KB of an un-parsed body (last resort)."""
    text = raw.decode("utf-8", "replace")
    text = re.sub(r"=\r?\n", "", text)
    text = re.sub(r"=([0-9A-F]{2})", lambda m: bytes.fromhex(m.group(1)).decode("latin-1"), text)
    lines = [
        ln for ln in text.splitlines()
        if ln.strip() and not ln.startswith("--") and not re.match(r"^[A-Za-z-]+: ", ln)
    ]
    return " ".join(html_to_text("\n".join(lines)).split())[:200]


# ── the backend ──────────────────────────────────────────────────────────────


@dataclass
class _Resource:
    """Enough of navig's ``Resource`` for the mailroom's ``search``/``fetch`` consumers."""

    id: str
    title: str = ""
    preview: str = ""
    url: str = ""
    timestamp: str = ""
    source: str = "gmail"
    metadata: dict[str, Any] = field(default_factory=dict)


class GmailImap:
    """The mailroom's Gmail backend over IMAP + SMTP (app password)."""

    def __init__(
        self,
        user: str,
        password: str,
        *,
        host: str = IMAP_HOST,
        port: int = 993,
        smtp_host: str = SMTP_HOST,
        smtp_port: int = 465,
        timeout: float = 30,
        imap_factory: Any = None,
        smtp_factory: Any = None,
    ) -> None:
        self.user = user.strip()
        self._password = password
        self.host, self.port = host, port
        self.smtp_host, self.smtp_port = smtp_host, smtp_port
        self.timeout = timeout
        self._imap_factory = imap_factory or (lambda: imaplib.IMAP4_SSL(host, port, timeout=timeout))
        self._smtp_factory = smtp_factory or (lambda: smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=timeout))
        self._conn: Any = None
        self._lock = threading.Lock()
        self._boxes: dict[str, str] = {}
        self._selected: str | None = None
        self._where: dict[str, tuple[str, int]] = {}  # message id -> (mailbox, uid)

    def __repr__(self) -> str:  # never show the password
        return f"GmailImap(user={self.user!r}, host={self.host!r})"

    # -- connection ---------------------------------------------------------

    def _connect(self) -> Any:
        if self._conn is not None:
            return self._conn
        conn = self._imap_factory()
        try:
            conn.login(self.user, self._password)
        except imaplib.IMAP4.error as exc:
            raise ConnectorAuthError(
                CONNECTOR_ID,
                "Gmail refused the sign-in. Use an APP PASSWORD (Google Account → Security → "
                f"App passwords), not your normal password. ({exc})",
            ) from None
        caps = {str(c).upper() for c in getattr(conn, "capabilities", ())}
        if "X-GM-EXT-1" not in caps:
            raise ImapNotGmail(
                CONNECTOR_ID, 501,
                f"{self.host} is not Gmail: navig-email's standalone mode speaks Gmail's IMAP "
                "(labels and Gmail search). Other providers are not supported yet.",
            )
        self._conn = conn
        self._boxes = self._special_use(conn)
        return conn

    @staticmethod
    def _special_use(conn: Any) -> dict[str, str]:
        """``{"\\All": "[Gmail]/All Mail", "\\Junk": …}`` — localised names come from LIST flags."""
        typ, data = conn.list()
        boxes: dict[str, str] = {}
        for raw in data or []:
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            m = re.match(r'\((?P<flags>[^)]*)\) (?:"[^"]*"|NIL) (?P<name>.+)$', line)
            if not m:
                continue
            name = m.group("name").strip()
            if name.startswith('"') and name.endswith('"'):
                name = name[1:-1].replace('\\"', '"')
            for flag in m.group("flags").split():
                if flag in ("\\All", "\\Junk", "\\Trash", "\\Sent", "\\Drafts", "\\Flagged", "\\Important"):
                    boxes[flag] = name
            boxes.setdefault("INBOX", "INBOX")
        if "\\All" not in boxes:
            raise ImapNotGmail(CONNECTOR_ID, 501, "no \\All mailbox: IMAP access to All Mail is off in Gmail settings")
        return boxes

    def _select(self, box: str) -> None:
        if self._selected == box:
            return
        typ, data = self._conn.select(f'"{box}"')
        if typ != "OK":
            raise ConnectorAPIError(CONNECTOR_ID, 404, f"cannot open mailbox {box}: {data}")
        self._selected = box

    def _cmd(self, *args: Any) -> list[Any]:
        typ, data = self._conn.uid(*args)
        if typ != "OK":
            raise ConnectorAPIError(CONNECTOR_ID, 400, f"{args[0]} failed: {data}")
        return data

    async def _run(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        def locked() -> Any:
            with self._lock:
                self._connect()
                return fn(*args, **kwargs)

        return await asyncio.to_thread(locked)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.logout()
                except Exception:  # noqa: BLE001
                    pass
                self._conn = None
                self._selected = None

    # -- search -------------------------------------------------------------

    def _boxes_for(self, label_ids: list[str] | None, include_spam_trash: bool) -> list[str]:
        ids = set(label_ids or [])
        if "SPAM" in ids:
            return [self._boxes["\\Junk"]] if "\\Junk" in self._boxes else []
        if "TRASH" in ids:
            return [self._boxes["\\Trash"]] if "\\Trash" in self._boxes else []
        boxes = [self._boxes["\\All"]]
        if include_spam_trash:
            boxes += [self._boxes[f] for f in ("\\Junk", "\\Trash") if f in self._boxes]
        return boxes

    @staticmethod
    def _label_terms(label_ids: list[str] | None) -> str:
        terms = []
        for lid in label_ids or []:
            if lid in ("SPAM", "TRASH"):
                continue
            if lid in ("INBOX", "SENT", "DRAFT", "STARRED", "IMPORTANT"):
                terms.append(f"in:{lid.lower()}" if lid != "STARRED" else "is:starred")
            elif lid == "UNREAD":
                terms.append("is:unread")
            else:
                terms.append(f'label:"{lid}"')
        return " ".join(terms)

    def _search_box(self, box: str, query: str) -> list[int]:
        self._select(box)
        q = query.strip()
        if not q:
            data = self._cmd("SEARCH", "ALL")
        elif q.isascii():
            data = self._cmd("SEARCH", "X-GM-RAW", '"' + q.replace("\\", "\\\\").replace('"', '\\"') + '"')
        else:
            self._conn.literal = q.encode("utf-8")
            data = self._cmd("SEARCH", "CHARSET", "UTF-8", "X-GM-RAW")
        raw = b" ".join(d for d in data if isinstance(d, bytes)).decode()
        return [int(u) for u in raw.split()]

    def _ids_for(self, box: str, uids: list[int]) -> list[dict[str, str]]:
        if not uids:
            return []
        self._select(box)
        out: dict[int, dict[str, str]] = {}
        for i in range(0, len(uids), 500):
            chunk = ",".join(str(u) for u in uids[i : i + 500])
            for rec in parse_fetch(self._cmd("FETCH", chunk, "(UID X-GM-MSGID X-GM-THRID)")):
                uid = _meta_int(rec["meta"], "UID")
                mid, tid = _meta_int(rec["meta"], "X-GM-MSGID"), _meta_int(rec["meta"], "X-GM-THRID")
                if uid is None or mid is None:
                    continue
                hid = format(mid, "x")
                self._where[hid] = (box, uid)
                out[uid] = {"id": hid, "threadId": format(tid or 0, "x")}
        return [out[u] for u in uids if u in out]

    def _iter_ids(self, query: str, limit: int | None, label_ids: list[str] | None,
                  include_spam_trash: bool) -> list[dict[str, str]]:
        q = " ".join(p for p in (query, self._label_terms(label_ids)) if p)
        found: list[dict[str, str]] = []
        seen: set[str] = set()
        for box in self._boxes_for(label_ids, include_spam_trash):
            uids = sorted(self._search_box(box, q), reverse=True)  # newest first
            if limit is not None:
                uids = uids[: max(0, limit - len(found))]
            for entry in self._ids_for(box, uids):
                if entry["id"] not in seen:
                    seen.add(entry["id"])
                    found.append(entry)
            if limit is not None and len(found) >= limit:
                break
        return found[:limit] if limit is not None else found

    async def list_message_ids(self, query: str = "", *, max_results: int = 500, page_token: str | None = None,
                               label_ids: list[str] | None = None,
                               include_spam_trash: bool = False) -> tuple[list[dict[str, str]], str | None]:
        return await self._run(self._iter_ids, query, max_results, label_ids, include_spam_trash), None

    async def iter_message_ids(self, query: str = "", *, limit: int | None = None,
                               label_ids: list[str] | None = None,
                               include_spam_trash: bool = False) -> list[dict[str, str]]:
        return await self._run(self._iter_ids, query, limit, label_ids, include_spam_trash)

    # -- messages -----------------------------------------------------------

    def _locate(self, message_id: str) -> tuple[str, int]:
        if message_id in self._where:
            return self._where[message_id]
        try:
            decimal = int(message_id, 16)
        except ValueError:
            raise ConnectorAPIError(CONNECTOR_ID, 400, f"not a Gmail message id: {message_id!r}") from None
        for flag in ("\\All", "\\Junk", "\\Trash"):
            box = self._boxes.get(flag)
            if not box:
                continue
            self._select(box)
            data = self._cmd("SEARCH", "X-GM-MSGID", str(decimal))
            uids = b" ".join(d for d in data if isinstance(d, bytes)).split()
            if uids:
                self._where[message_id] = (box, int(uids[0]))
                return self._where[message_id]
        raise ConnectorAPIError(CONNECTOR_ID, 404, f"message {message_id} not found")

    def _labels_of(self, meta: str, box: str) -> list[str]:
        labels: list[str] = []
        for raw in _meta_list(meta, "X-GM-LABELS"):
            low = raw.lower()
            if low in _SYSTEM_IMAP_TO_API:
                labels.append(_SYSTEM_IMAP_TO_API[low])
            elif raw and not raw.startswith("\\"):
                labels.append(decode_mutf7(raw))
        flags = [f.lower() for f in _meta_list(meta, "FLAGS")]
        if "\\seen" not in flags:
            labels.append("UNREAD")
        if "\\flagged" in flags and "STARRED" not in labels:
            labels.append("STARRED")
        if box == self._boxes.get("\\Junk") and "SPAM" not in labels:
            labels.append("SPAM")
        if box == self._boxes.get("\\Trash") and "TRASH" not in labels:
            labels.append("TRASH")
        return labels

    def _get(self, message_id: str, fmt: str, headers: tuple[str, ...] | None) -> dict[str, Any]:
        box, uid = self._locate(message_id)
        self._select(box)
        items = "UID X-GM-MSGID X-GM-THRID X-GM-LABELS FLAGS INTERNALDATE RFC822.SIZE"
        if fmt == "full":
            items += " BODY.PEEK[]"
        elif fmt == "metadata":
            items += " BODY.PEEK[HEADER] BODY.PEEK[TEXT]<0.1500>"
        recs = parse_fetch(self._cmd("FETCH", str(uid), f"({items})"))
        if not recs:
            raise ConnectorAPIError(CONNECTOR_ID, 404, f"message {message_id} not found")
        rec = recs[0]
        meta = rec["meta"]
        out: dict[str, Any] = {
            "id": message_id,
            "threadId": format(_meta_int(meta, "X-GM-THRID") or 0, "x"),
            "labelIds": self._labels_of(meta, box),
            "internalDate": str(_internal_ms(meta)),
            "sizeEstimate": _meta_int(meta, "RFC822.SIZE") or 0,
            "snippet": "",
        }
        if fmt == "full":
            msg = email.message_from_bytes(rec["sections"].get("BODY[]", b""), policy=email.policy.compat32)
            out["payload"] = _payload(msg)
            out["snippet"] = _snippet_from(msg)
        elif fmt == "metadata":
            header = rec["sections"].get("BODY[HEADER]", b"")
            msg = email.message_from_bytes(header, policy=email.policy.compat32)
            out["payload"] = {"mimeType": msg.get_content_type(),
                              "headers": _headers(msg, tuple(headers or DEFAULT_METADATA_HEADERS))}
            text = rec["sections"].get("BODY[TEXT]", b"")
            out["snippet"] = _partial_snippet(header, text, truncated=len(text) >= 1500)
        return out

    async def get_message(self, message_id: str, *, format: str = "metadata",
                          headers: list[str] | tuple[str, ...] | None = None) -> dict[str, Any]:
        return await self._run(self._get, message_id, format, tuple(headers) if headers else None)

    async def get_many(self, message_ids: list[str], *, format: str = "metadata",
                       headers: list[str] | tuple[str, ...] | None = None, concurrency: int = 8) -> list[dict[str, Any]]:
        def many() -> list[dict[str, Any]]:
            out = []
            for mid in message_ids:
                try:
                    out.append(self._get(mid, format, tuple(headers) if headers else None))
                except ConnectorAPIError:
                    continue  # failed ids are skipped, as the OAuth connector does
            return out

        return await self._run(many)

    def _thread(self, thread_id: str, fmt: str, headers: tuple[str, ...] | None) -> dict[str, Any]:
        decimal = int(thread_id, 16)
        ids: list[str] = []
        for flag in ("\\All", "\\Junk", "\\Trash"):
            box = self._boxes.get(flag)
            if not box:
                continue
            self._select(box)
            data = self._cmd("SEARCH", "X-GM-THRID", str(decimal))
            uids = sorted(int(u) for u in b" ".join(d for d in data if isinstance(d, bytes)).split())
            ids += [e["id"] for e in self._ids_for(box, uids) if e["id"] not in ids]
        msgs = [self._get(mid, fmt, headers) for mid in ids]
        msgs.sort(key=lambda m: int(m.get("internalDate") or 0))
        return {"id": thread_id, "messages": msgs}

    async def get_thread(self, thread_id: str, *, format: str = "metadata",
                         headers: list[str] | tuple[str, ...] | None = None) -> dict[str, Any]:
        return await self._run(self._thread, thread_id, format, tuple(headers) if headers else None)

    async def get_threads(self, thread_ids: list[str], *, format: str = "metadata",
                          headers: list[str] | tuple[str, ...] | None = None, concurrency: int = 8) -> list[dict[str, Any]]:
        def many() -> list[dict[str, Any]]:
            out = []
            for tid in thread_ids:
                try:
                    out.append(self._thread(tid, format, tuple(headers) if headers else None))
                except (ConnectorAPIError, ValueError):
                    continue
            return out

        return await self._run(many)

    # -- profile / history (the watch) ---------------------------------------

    def _watermark(self) -> tuple[int, int, int]:
        box = self._boxes["\\All"]
        typ, data = self._conn.status(f'"{box}"', "(MESSAGES UIDNEXT UIDVALIDITY)")
        text = b" ".join(d for d in data if isinstance(d, bytes)).decode("utf-8", "replace")
        return (_meta_int(text, "MESSAGES") or 0, _meta_int(text, "UIDNEXT") or 0, _meta_int(text, "UIDVALIDITY") or 0)

    async def get_profile(self) -> dict[str, Any]:
        def prof() -> dict[str, Any]:
            total, uidnext, validity = self._watermark()
            return {"emailAddress": self.user, "messagesTotal": total,
                    "historyId": f"{HISTORY_PREFIX}{validity}:{uidnext}"}

        return await self._run(prof)

    async def list_history(self, start_history_id: str, *, label_id: str | None = None,
                           history_types: tuple[str, ...] = ("messageAdded",),
                           page_token: str | None = None) -> dict[str, Any]:
        """New mail since a watermark ``imap:<UIDVALIDITY>:<UIDNEXT>`` of All Mail.

        A watermark from another backend, or one whose UIDVALIDITY changed, is "too old":
        ``ConnectorAPIError(404)``, the exact signal the watch already turns into a
        time-window search.
        """

        def hist() -> dict[str, Any]:
            _total, uidnext, validity = self._watermark()
            m = re.fullmatch(rf"{HISTORY_PREFIX}(\d+):(\d+)", str(start_history_id or ""))
            if not m or int(m.group(1)) != validity:
                raise ConnectorAPIError(CONNECTOR_ID, 404, "history cursor is not valid for this mailbox")
            start = int(m.group(2))
            box = self._boxes["\\All"]
            added: list[dict[str, Any]] = []
            if uidnext > start:
                self._select(box)
                uids = sorted(int(u) for u in b" ".join(
                    d for d in self._cmd("SEARCH", "UID", f"{start}:*") if isinstance(d, bytes)).split()
                    if int(u) >= start)
                for entry in self._ids_for(box, uids):
                    msg = self._get(entry["id"], "minimal", None)
                    if label_id and label_id not in msg["labelIds"]:
                        continue
                    added.append({"message": {"id": msg["id"], "threadId": msg["threadId"], "labelIds": msg["labelIds"]}})
            history = [{"id": f"{validity}:{uidnext}", "messagesAdded": added}] if added else []
            return {"history": history, "historyId": f"{HISTORY_PREFIX}{validity}:{uidnext}"}

        return await self._run(hist)

    # -- labels -------------------------------------------------------------

    def _labels(self) -> list[dict[str, Any]]:
        typ, data = self._conn.list()
        out = [{"id": lid, "name": lid, "type": "system"} for lid in SYSTEM_LABEL_IDS]
        for raw in data or []:
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            m = re.match(r'\((?P<flags>[^)]*)\) (?:"[^"]*"|NIL) (?P<name>.+)$', line)
            if not m or "\\Noselect" in m.group("flags"):
                continue
            name = m.group("name").strip().strip('"')
            if name == "INBOX" or name.startswith("[Gmail]"):
                continue
            label = decode_mutf7(name)
            out.append({"id": label, "name": label, "type": "user"})
        return out

    async def list_labels(self) -> list[dict[str, Any]]:
        return await self._run(self._labels)

    async def ensure_label(self, name: str) -> str:
        wanted = name.strip().strip("/")
        if not wanted:
            raise ValueError("label name is empty")
        if wanted.upper() in SYSTEM_LABEL_IDS:
            return wanted.upper()

        def ensure() -> str:
            existing = {lbl["name"].lower(): lbl["id"] for lbl in self._labels()}
            if wanted.lower() in existing:
                return existing[wanted.lower()]
            typ, data = self._conn.create(f'"{encode_mutf7(wanted)}"')
            if typ != "OK":
                raise ConnectorAPIError(CONNECTOR_ID, 400, f"cannot create label {wanted!r}: {data}")
            return wanted

        return await self._run(ensure)

    def _modify(self, message_id: str, add: list[str] | None, remove: list[str] | None) -> ActionResult:
        if not message_id:
            return ActionResult(success=False, error="message_id required")
        box, uid = self._locate(message_id)
        self._select(box)

        def labels_arg(ids: list[str]) -> str:
            parts = []
            for lid in ids:
                if lid in _SYSTEM_API_TO_IMAP:
                    parts.append(_SYSTEM_API_TO_IMAP[lid])
                else:
                    parts.append('"' + encode_mutf7(lid).replace('"', '\\"') + '"')
            return "(" + " ".join(parts) + ")"

        add, remove = list(add or []), list(remove or [])
        flag_add = [f for lid, f in (("STARRED", "\\Flagged"),) if lid in add] + (["\\Seen"] if "UNREAD" in remove else [])
        flag_del = [f for lid, f in (("STARRED", "\\Flagged"),) if lid in remove] + (["\\Seen"] if "UNREAD" in add else [])
        label_add = [lid for lid in add if lid not in ("UNREAD", "STARRED")]
        label_del = [lid for lid in remove if lid not in ("UNREAD", "STARRED")]
        try:
            if flag_add:
                self._cmd("STORE", str(uid), "+FLAGS", "(" + " ".join(flag_add) + ")")
            if flag_del:
                self._cmd("STORE", str(uid), "-FLAGS", "(" + " ".join(flag_del) + ")")
            if label_add:
                self._cmd("STORE", str(uid), "+X-GM-LABELS", labels_arg(label_add))
            if label_del:
                self._cmd("STORE", str(uid), "-X-GM-LABELS", labels_arg(label_del))
        except ConnectorAPIError as exc:
            return ActionResult(success=False, error=str(exc))
        if "TRASH" in add or "SPAM" in add:
            self._where.pop(message_id, None)  # it left All Mail
        return ActionResult(success=True)

    async def modify_message(self, message_id: str, *, add: list[str] | None = None,
                             remove: list[str] | None = None) -> ActionResult:
        return await self._run(self._modify, message_id, add, remove)

    async def modify_thread(self, thread_id: str, *, add: list[str] | None = None,
                            remove: list[str] | None = None) -> ActionResult:
        if not thread_id:
            return ActionResult(success=False, error="thread_id required")

        def mod() -> ActionResult:
            thread = self._thread(thread_id, "minimal", None)
            for msg in thread["messages"]:
                res = self._modify(msg["id"], add, remove)
                if not res.success:
                    return res
            return ActionResult(success=True)

        return await self._run(mod)

    # -- drafts / send --------------------------------------------------------

    async def create_draft(self, *, to: str, subject: str, body: str, thread_id: str | None = None,
                           in_reply_to: str | None = None, references: str | None = None) -> dict[str, Any]:
        mime = MIMEText(body, "plain", "utf-8")
        mime["To"], mime["Subject"], mime["From"] = to, subject, self.user
        if in_reply_to:
            mime["In-Reply-To"] = in_reply_to
            mime["References"] = references or in_reply_to

        def draft() -> dict[str, Any]:
            box = self._boxes.get("\\Drafts")
            if not box:
                raise ConnectorAPIError(CONNECTOR_ID, 404, "no Drafts mailbox")
            typ, data = self._conn.append(f'"{box}"', "(\\Draft)", imaplib.Time2Internaldate(time.time()),
                                          mime.as_bytes())
            if typ != "OK":
                raise ConnectorAPIError(CONNECTOR_ID, 400, f"could not save the draft: {data}")
            return {"id": "", "message": {"threadId": thread_id or ""}}

        return await self._run(draft)

    def _smtp_send(self, mime: MIMEText) -> None:
        with self._smtp_factory() as smtp:
            smtp.login(self.user, self._password)
            smtp.send_message(mime)

    async def act(self, action: Any) -> ActionResult:
        kind = getattr(action.action_type, "value", action.action_type)
        params = getattr(action, "params", {}) or {}
        rid = getattr(action, "resource_id", None) or ""
        try:
            if kind == ActionType.SEND.value:
                mime = MIMEText(params.get("body", ""), "plain", "utf-8")
                mime["To"], mime["Subject"], mime["From"] = params.get("to", ""), params.get("subject", ""), self.user
                await asyncio.to_thread(self._smtp_send, mime)
                return ActionResult(success=True, resource=_Resource(id="", title=f"Sent: {params.get('subject', '')}"))
            if kind == ActionType.REPLY.value:
                if not rid:
                    return ActionResult(success=False, error="resource_id required for reply")
                orig = await self.get_message(rid, format="metadata", headers=["Subject", "From", "Message-ID"])
                hdr = {h["name"].lower(): h["value"] for h in orig.get("payload", {}).get("headers", [])}
                subject = hdr.get("subject", "")
                if not subject.lower().startswith("re:"):
                    subject = f"Re: {subject}"
                mime = MIMEText(params.get("body", ""), "plain", "utf-8")
                mime["To"], mime["Subject"], mime["From"] = hdr.get("from", ""), subject, self.user
                if hdr.get("message-id"):
                    mime["In-Reply-To"] = mime["References"] = hdr["message-id"]
                await asyncio.to_thread(self._smtp_send, mime)
                return ActionResult(success=True, resource=_Resource(id="", title=subject))
            if kind == ActionType.ARCHIVE.value:
                return await self.modify_message(rid, remove=["INBOX"])
            if kind == ActionType.LABEL.value:
                return await self.modify_message(rid, add=params.get("add_labels", []),
                                                 remove=params.get("remove_labels", []))
            if kind == ActionType.DELETE.value:
                return await self.modify_message(rid, add=["TRASH"])
            return ActionResult(success=False, error=f"Unsupported action: {kind}")
        except (ConnectorAPIError, smtplib.SMTPException, OSError) as exc:
            return ActionResult(success=False, error=str(exc))

    # -- generic connector surface (search / fetch) ---------------------------

    async def search(self, query: str, limit: int = 5) -> list[_Resource]:
        ids = await self.iter_message_ids(query, limit=limit)
        msgs = await self.get_many([e["id"] for e in ids])
        out = []
        for m in msgs:
            hdr = {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}
            out.append(_Resource(id=m["id"], title=hdr.get("subject", ""), preview=m.get("snippet", ""),
                                 url=f"https://mail.google.com/mail/#all/{m['id']}",
                                 metadata={"from": hdr.get("from", ""), "to": hdr.get("to", ""),
                                           "labels": m.get("labelIds", [])}))
        return out

    async def fetch(self, resource_id: str) -> _Resource:
        m = await self.get_message(resource_id, format="full")
        hdr = {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}
        body = self._extract_body(m.get("payload", {}))
        return _Resource(id=m["id"], title=hdr.get("subject", ""), preview=body[:500],
                         url=f"https://mail.google.com/mail/#all/{m['id']}",
                         metadata={"from": hdr.get("from", ""), "to": hdr.get("to", ""), "body": body,
                                   "labels": m.get("labelIds", [])})

    # -- body extraction (identical contract to the OAuth connector's) --------

    @staticmethod
    def _decode_part(payload: dict[str, Any]) -> str:
        data = payload.get("body", {}).get("data", "")
        if not data:
            return ""
        try:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _first_part(payload: dict[str, Any], mime_type: str) -> str:
        if payload.get("mimeType", "") == mime_type:
            text = GmailImap._decode_part(payload)
            if text:
                return text
        for part in payload.get("parts", []):
            text = GmailImap._first_part(part, mime_type)
            if text:
                return text
        return ""

    @staticmethod
    def _extract_body(payload: dict[str, Any]) -> str:
        plain = GmailImap._first_part(payload, "text/plain")
        if plain:
            return plain
        markup = GmailImap._first_part(payload, "text/html")
        return html_to_text(markup) if markup else ""
