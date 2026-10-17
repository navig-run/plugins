"""The Gmail-over-IMAP backend: same methods, same raw-Gmail-JSON shapes as the OAuth connector.

A fake Gmail IMAP server holds real RFC 822 messages and answers in imaplib's exact response
structure (``uid`` returns ``(typ, data)`` with ``(meta, literal)`` tuples). The parser is also
checked against the literal examples in Google's IMAP-extension documentation, so it is not
only tested against a format this file invented.
"""

from __future__ import annotations

import asyncio
import re
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytest

from navig_email import imap as I
from navig_email.errors import Action, ActionType, ConnectorAPIError, ConnectorAuthError

ALL, JUNK, TRASH, DRAFTS = "[Gmail]/All Mail", "[Gmail]/Spam", "[Gmail]/Corbeille", "[Gmail]/Drafts"


def _raw(subject: str, sender: str, body: str, *, html: bool = False) -> bytes:
    if html:
        m = MIMEMultipart("alternative")
        m.attach(MIMEText(f"<p>{body}</p>", "html", "utf-8"))
    else:
        m = MIMEText(body, "plain", "utf-8")
    m["Subject"], m["From"], m["To"], m["Message-ID"] = subject, sender, "me@gmail.com", f"<{abs(hash(subject))}@x>"
    return m.as_bytes()


class FakeGmail:
    """In-memory Gmail IMAP. Messages: uid -> dict(msgid, thrid, labels, flags, box, raw)."""

    capabilities = ("IMAP4REV1", "X-GM-EXT-1", "UIDPLUS")

    def __init__(self, password: str = "app-pass") -> None:
        self.password = password
        self.selected = None
        self.literal = None
        self.stored: list[tuple] = []
        self.created: list[str] = []
        self.appended: list[tuple] = []
        self.msgs: dict[str, dict[int, dict]] = {ALL: {}, JUNK: {}, TRASH: {}, DRAFTS: {}}
        self.user_labels = ["Factures", "Clients/2026", "Réunions"]

    def add(self, box: str, uid: int, msgid: int, thrid: int, raw: bytes, labels=(), flags=()):
        self.msgs[box][uid] = {"msgid": msgid, "thrid": thrid, "labels": list(labels), "flags": list(flags), "raw": raw}

    # -- imaplib surface --
    def login(self, user, password):
        if password != self.password:
            raise I.imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")
        return "OK", [b"Success"]

    def list(self):
        lines = [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasChildren \\Noselect) "/" "[Gmail]"',
                 b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"', b'(\\HasNoChildren \\Junk) "/" "[Gmail]/Spam"',
                 b'(\\HasNoChildren \\Trash) "/" "[Gmail]/Corbeille"', b'(\\Drafts \\HasNoChildren) "/" "[Gmail]/Drafts"']
        lines += [f'(\\HasNoChildren) "/" "{I.encode_mutf7(n)}"'.encode() for n in self.user_labels]
        return "OK", lines

    def select(self, box, readonly=False):
        self.selected = box.strip('"')
        return ("OK", [str(len(self.msgs.get(self.selected, {}))).encode()]) if self.selected in self.msgs else ("NO", [b"no"])

    def status(self, box, items):
        box = box.strip('"')
        uids = self.msgs[box].keys()
        return "OK", [f'"{box}" (MESSAGES {len(uids)} UIDNEXT {max(uids, default=0) + 1} UIDVALIDITY 7)'.encode()]

    def create(self, box):
        self.created.append(I.decode_mutf7(box.strip('"')))
        return "OK", [b"created"]

    def append(self, box, flags, date, raw):
        self.appended.append((box.strip('"'), flags, raw))
        return "OK", [b"[APPENDUID 7 99] done"]

    def logout(self):
        return "BYE", []

    def _match(self, m: dict, q: str) -> bool:
        from email import message_from_bytes

        msg = message_from_bytes(m["raw"])
        for term in re.findall(r'(\w+:"[^"]*"|\S+)', q):
            if term.startswith("from:"):
                if term[5:].lower() not in str(msg["From"]).lower():
                    return False
            elif term == "in:inbox":
                if "\\Inbox" not in m["labels"]:
                    return False
            elif term == "is:unread":
                if "\\Seen" in m["flags"]:
                    return False
            elif term.startswith("label:"):
                if term[6:].strip('"') not in [I.decode_mutf7(x) for x in m["labels"]]:
                    return False
            elif term.lower() not in msg.as_string().lower():
                return False
        return True

    def uid(self, cmd, *args):
        box = self.msgs[self.selected]
        if cmd == "SEARCH":
            if args[0] == "ALL":
                hits = list(box)
            elif args[0] == "X-GM-MSGID":
                hits = [u for u, m in box.items() if m["msgid"] == int(args[1])]
            elif args[0] == "X-GM-THRID":
                hits = [u for u, m in box.items() if m["thrid"] == int(args[1])]
            elif args[0] == "UID":
                lo = int(args[1].split(":")[0])
                hits = [u for u in box if u >= lo] or ([max(box)] if box else [])
            else:
                if args[0] == "CHARSET":
                    q, self.literal = self.literal.decode("utf-8"), None
                else:
                    q = args[1][1:-1].replace('\\"', '"')
                hits = [u for u, m in box.items() if self._match(m, q)]
            return "OK", [" ".join(str(u) for u in sorted(hits)).encode()]
        if cmd == "FETCH":
            uids = [int(u) for u in args[0].split(",")]
            items = args[1]
            data: list = []
            for seq, uid in enumerate(uids, 1):
                if uid not in box:
                    continue
                m = box[uid]
                labels = " ".join(x if x.startswith("\\") else f'"{x}"' for x in m["labels"])
                meta = (f'{seq} (X-GM-THRID {m["thrid"]} X-GM-MSGID {m["msgid"]} X-GM-LABELS ({labels}) '
                        f'UID {uid} FLAGS ({" ".join(m["flags"])}) INTERNALDATE "21-Sep-2026 09:15:02 +0200" '
                        f'RFC822.SIZE {len(m["raw"])}')
                if "BODY.PEEK[]" in items:
                    data += [(f"{meta} BODY[] {{{len(m['raw'])}}}".encode(), m["raw"]), b")"]
                elif "BODY.PEEK[HEADER]" in items:
                    head, _, text = m["raw"].partition(b"\n\n")
                    data += [(f"{meta} BODY[HEADER] {{{len(head)}}}".encode(), head + b"\n\n"),
                             (f" BODY[TEXT]<0> {{{len(text[:1500])}}}".encode(), text[:1500]), b")"]
                else:
                    data.append(f"{meta})".encode())
            return "OK", data
        if cmd == "STORE":
            uid, op, value = int(args[0]), args[1], args[2]
            self.stored.append((uid, op, value))
            items, _ = I._tokenize_list(value, 0)
            key = "labels" if "X-GM-LABELS" in op else "flags"
            for it in items:
                if op.startswith("+") and it not in box[uid][key]:
                    box[uid][key].append(it)
                if op.startswith("-") and it in box[uid][key]:
                    box[uid][key].remove(it)
            return "OK", [b"stored"]
        raise AssertionError(f"unexpected UID {cmd}")


@pytest.fixture
def fake():
    f = FakeGmail()
    f.add(ALL, 11, 0x18A1, 0x18A1, _raw("Facture CAF septembre", "CAF <noreply@caf.fr>", "Montant: 312 EUR"),
          labels=["\\Inbox", "Factures"])
    f.add(ALL, 12, 0x18B2, 0x18A1, _raw("Re: Facture CAF septembre", "me@gmail.com", "Merci"), labels=["\\Sent"],
          flags=["\\Seen"])
    f.add(ALL, 13, 0x18C3, 0x18C3, _raw("Newsletter", "News <news@x.io>", "Big <b>sale</b> today", html=True),
          labels=["\\Inbox"], flags=["\\Seen", "\\Flagged"])
    f.add(JUNK, 3, 0x18D4, 0x18D4, _raw("You won", "spam@bad.biz", "claim"))
    return f


@pytest.fixture
def gm(fake):
    return I.GmailImap("me@gmail.com", "app-pass", imap_factory=lambda: fake, smtp_factory=None)


def run(coro):
    return asyncio.run(coro)


# ── parser vs Google's documented examples ───────────────────────────────────


def test_parser_reads_googles_documented_fetch_responses():
    # https://developers.google.com/gmail/imap/imap-extensions (imaplib strips "* " and "FETCH ")
    data = [b"1 (X-GM-LABELS (\\Inbox) UID 302)",
            b'2 (X-GM-LABELS (\\Inbox \\Sent Important "Muy Importante") UID 303)',
            b"3 (X-GM-MSGID 1278455344230334865 X-GM-THRID 1278455344230334865 UID 304)"]
    recs = I.parse_fetch(data)
    assert [I._meta_int(r["meta"], "UID") for r in recs] == [302, 303, 304]
    assert I._meta_list(recs[1]["meta"], "X-GM-LABELS") == ["\\Inbox", "\\Sent", "Important", "Muy Importante"]
    assert I._meta_int(recs[2]["meta"], "X-GM-MSGID") == 1278455344230334865


def test_modified_utf7_round_trip():
    for name in ("Réunions", "Clients/2026", "a&b", "日本語", "plain"):
        assert I.decode_mutf7(I.encode_mutf7(name)) == name
    assert I.encode_mutf7("a&b") == "a&-b"
    assert I.decode_mutf7("R&AOk-unions") == "Réunions"  # the RFC 3501 spelling


# ── the connector surface ────────────────────────────────────────────────────


def test_search_ids_are_the_gmail_api_hex_ids_newest_first(gm):
    ids = run(gm.iter_message_ids("from:caf.fr"))
    assert ids == [{"id": "18a1", "threadId": "18a1"}]
    everything = run(gm.iter_message_ids("", limit=10))
    assert [e["id"] for e in everything] == ["18c3", "18b2", "18a1"]  # newest (highest UID) first


def test_label_ids_and_spam_scope(gm):
    assert [e["id"] for e in run(gm.iter_message_ids("", label_ids=["INBOX"]))] == ["18c3", "18a1"]
    assert [e["id"] for e in run(gm.iter_message_ids("", label_ids=["SPAM"]))] == ["18d4"]
    assert [e["id"] for e in run(gm.iter_message_ids("won", include_spam_trash=True))] == ["18d4"]
    assert run(gm.iter_message_ids("won")) == []  # spam is not searched unless asked


def test_get_message_metadata_shape(gm):
    m = run(gm.get_message("18a1"))
    assert m["threadId"] == "18a1"
    assert set(m["labelIds"]) == {"INBOX", "Factures", "UNREAD"}
    hdr = {h["name"]: h["value"] for h in m["payload"]["headers"]}
    assert hdr["Subject"] == "Facture CAF septembre" and "caf.fr" in hdr["From"]
    assert m["internalDate"] == str(1789974902000)  # 21-Sep-2026 09:15:02 +0200 = 07:15:02 UTC
    assert "312 EUR" in m["snippet"]


def test_get_message_full_body_and_html_fallback(gm):
    m = run(gm.get_message("18c3", format="full"))
    assert set(m["labelIds"]) == {"INBOX", "STARRED"}  # \Seen -> not UNREAD; \Flagged -> STARRED
    assert gm._extract_body(m["payload"]) == "Big sale today"


def test_thread_collects_every_message_in_order(gm):
    t = run(gm.get_thread("18a1"))
    assert [m["id"] for m in t["messages"]] == ["18a1", "18b2"]


def test_labels_list_decodes_names_and_ensure_creates(gm, fake):
    names = {lbl["name"] for lbl in run(gm.list_labels())}
    assert {"INBOX", "UNREAD", "Factures", "Clients/2026", "Réunions"} <= names
    assert not any(n.startswith("[Gmail]") for n in names)
    assert run(gm.ensure_label("Factures")) == "Factures"
    assert run(gm.ensure_label("Impôts/2026")) == "Impôts/2026"
    assert fake.created == ["Impôts/2026"]
    assert run(gm.ensure_label("inbox")) == "INBOX"


def test_modify_maps_system_labels_to_flags(gm, fake):
    assert run(gm.modify_message("18a1", add=["Clients/2026", "STARRED"], remove=["UNREAD", "INBOX"])).success
    m = fake.msgs[ALL][11]
    assert "\\Seen" in m["flags"] and "\\Flagged" in m["flags"]
    assert "\\Inbox" not in m["labels"] and "Clients/2026" in m["labels"]
    assert set(run(gm.get_message("18a1"))["labelIds"]) == {"Factures", "Clients/2026", "STARRED"}


def test_history_watermark_and_expired_cursor(gm, fake):
    prof = run(gm.get_profile())
    assert prof["emailAddress"] == "me@gmail.com" and prof["messagesTotal"] == 3
    cursor = prof["historyId"]
    assert run(gm.list_history(cursor))["history"] == []
    fake.add(ALL, 14, 0x18E5, 0x18E5, _raw("Nouveau", "a@b.c", "hi"), labels=["\\Inbox"])
    h = run(gm.list_history(cursor, label_id="INBOX"))
    assert [a["message"]["id"] for a in h["history"][0]["messagesAdded"]] == ["18e5"]
    assert h["historyId"] != cursor
    with pytest.raises(ConnectorAPIError) as exc:  # the watch falls back on 404
        run(gm.list_history("12345"))
    assert exc.value.status_code == 404


def test_draft_appends_to_the_localised_drafts_mailbox(gm, fake):
    run(gm.create_draft(to="x@y.z", subject="Re: hi", body="ok", in_reply_to="<m@x>"))
    box, flags, raw = fake.appended[0]
    assert box == DRAFTS and flags == "(\\Draft)" and b"In-Reply-To: <m@x>" in raw


def test_send_goes_through_smtp_with_the_app_password(fake):
    sent = []

    class _Smtp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, user, password):
            sent.append(("login", user, password))

        def send_message(self, mime):
            sent.append(("send", mime["To"], mime["Subject"], mime["From"]))

    gm = I.GmailImap("me@gmail.com", "app-pass", imap_factory=lambda: fake, smtp_factory=_Smtp)
    res = run(gm.act(Action(action_type=ActionType.SEND, params={"to": "a@b.c", "subject": "Hi", "body": "x"})))
    assert res.success and sent == [("login", "me@gmail.com", "app-pass"), ("send", "a@b.c", "Hi", "me@gmail.com")]
    res = run(gm.act(Action(action_type=ActionType.REPLY, resource_id="18a1", params={"body": "ok"})))
    assert res.success and sent[-1][2] == "Re: Facture CAF septembre" and "caf.fr" in sent[-1][1]


def test_wrong_password_names_the_app_password():
    gm = I.GmailImap("me@gmail.com", "normal-password", imap_factory=FakeGmail)
    with pytest.raises(ConnectorAuthError, match="APP PASSWORD"):
        run(gm.get_profile())


def test_a_server_that_is_not_gmail_is_refused_not_half_used():
    class NotGmail(FakeGmail):
        capabilities = ("IMAP4REV1",)

    gm = I.GmailImap("me@example.org", "app-pass", imap_factory=NotGmail)
    with pytest.raises(I.ImapNotGmail, match="not Gmail"):
        run(gm.get_profile())


def test_repr_never_shows_the_password(gm):
    assert "app-pass" not in repr(gm)


def test_snippet_decodes_base64_and_quoted_printable_even_when_cut(fake, gm):
    """Measured: a base64 UTF-8 body's snippet read as raw base64 ('TW9udGFudDog…')."""
    long_body = "Échéance du loyer : 850 € — " + "détail " * 400
    qp = MIMEText(long_body, "plain", "utf-8")
    qp.replace_header("Content-Transfer-Encoding", "quoted-printable")
    import quopri

    qp.set_payload(quopri.encodestring(long_body.encode("utf-8")).decode("ascii"))
    qp["Subject"], qp["From"] = "Loyer", "agence@immo.fr"
    fake.add(ALL, 20, 0x19F0, 0x19F0, qp.as_bytes(), labels=["\Inbox"])
    b64 = MIMEText(long_body, "plain", "utf-8")  # base64 by default, and longer than 1500 bytes
    b64["Subject"], b64["From"] = "Loyer 2", "agence@immo.fr"
    fake.add(ALL, 21, 0x19F1, 0x19F1, b64.as_bytes(), labels=["\Inbox"])
    for mid in ("19f0", "19f1"):
        snip = run(gm.get_message(mid))["snippet"]
        assert snip.startswith("Échéance du loyer : 850 €"), (mid, snip[:60])
